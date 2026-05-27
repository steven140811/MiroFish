"""
本地图谱存储与抽取服务。

使用 SQLite 在本地持久化图谱、本体、节点和边，
并通过现有 LLMClient 对文本块进行结构化抽取。
"""

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..config import Config
from ..utils.llm_client import LLMClient
from ..utils.logger import get_logger

logger = get_logger('mirofish.local_graph')


class LocalGraphStore:
    """SQLite 驱动的本地图谱存储。"""

    _UUID_NAMESPACE = uuid.UUID('4f08b7c2-5803-4cb5-9257-e9f4f8660d0a')
    _CJK_PATTERN = re.compile(r'[\u4e00-\u9fff]')
    _ASCII_PAREN_PATTERN = re.compile(r'\s*[（(][A-Za-z0-9 .,&/\-]+[）)]\s*')
    _ZH_ALIAS_BY_KEY = {
        'china': '中国',
        'mainland china': '中国',
        'prc': '中国',
        "people's republic of china": '中国',
        'peoples republic of china': '中国',
        'zhongguo': '中国',
        'united states': '美国',
        'united states of america': '美国',
        'usa': '美国',
        'u.s.': '美国',
        'u.s.a.': '美国',
        'us': '美国',
        'america': '美国',
        'russia': '俄罗斯',
        'russian federation': '俄罗斯',
        'european union': '欧盟',
        'eu': '欧盟',
        'india': '印度',
        'japan': '日本',
        'south korea': '韩国',
        'republic of korea': '韩国',
        'north korea': '朝鲜',
        'taiwan': '台湾',
        'hong kong': '香港',
        'singapore': '新加坡',
        'asean': '东盟',
        'nato': '北约',
        'united nations': '联合国',
        'un': '联合国',
        'world bank': '世界银行',
        'imf': '国际货币基金组织',
        'international monetary fund': '国际货币基金组织',
        'wto': '世界贸易组织',
        'world trade organization': '世界贸易组织',
        'wipo': '世界知识产权组织',
        'world intellectual property organization': '世界知识产权组织',
        'sipri': '斯德哥尔摩国际和平研究所',
        'stockholm international peace research institute': '斯德哥尔摩国际和平研究所',
        'stanford hai': '斯坦福 HAI',
        'stanford human centered ai institute': '斯坦福 HAI',
    }

    def __init__(
        self,
        db_path: Optional[str] = None,
        llm_client: Optional[LLMClient] = None,
    ):
        self.db_path = db_path or Config.GRAPH_DB_PATH
        self._llm_client = llm_client
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._initialize_database()

    @property
    def llm(self) -> LLMClient:
        if self._llm_client is None:
            self._llm_client = LLMClient()
        return self._llm_client

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_database(self):
        with self._connect() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA synchronous=NORMAL')
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS graphs (
                    graph_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    ontology_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS nodes (
                    uuid TEXT PRIMARY KEY,
                    graph_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    labels_json TEXT NOT NULL,
                    summary TEXT DEFAULT '',
                    attributes_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS edges (
                    uuid TEXT PRIMARY KEY,
                    graph_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    fact TEXT DEFAULT '',
                    fact_type TEXT DEFAULT '',
                    source_node_uuid TEXT NOT NULL,
                    target_node_uuid TEXT NOT NULL,
                    source_node_name TEXT DEFAULT '',
                    target_node_name TEXT DEFAULT '',
                    attributes_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    valid_at TEXT,
                    invalid_at TEXT,
                    expired_at TEXT,
                    episodes_json TEXT DEFAULT '[]',
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                'CREATE INDEX IF NOT EXISTS idx_nodes_graph_id ON nodes(graph_id, uuid)'
            )
            connection.execute(
                'CREATE INDEX IF NOT EXISTS idx_nodes_graph_name ON nodes(graph_id, name COLLATE NOCASE)'
            )
            connection.execute(
                'CREATE INDEX IF NOT EXISTS idx_edges_graph_id ON edges(graph_id, uuid)'
            )
            connection.execute(
                'CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_node_uuid)'
            )
            connection.execute(
                'CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_node_uuid)'
            )
            connection.commit()

    def create_graph(self, graph_id: str, name: str, description: str = ''):
        now = datetime.utcnow().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO graphs(graph_id, name, description, ontology_json, created_at, updated_at)
                VALUES(?, ?, ?, COALESCE((SELECT ontology_json FROM graphs WHERE graph_id = ?), '{}'),
                       COALESCE((SELECT created_at FROM graphs WHERE graph_id = ?), ?), ?)
                """,
                (graph_id, name, description, graph_id, graph_id, now, now),
            )
            connection.commit()

    def set_ontology(self, graph_id: str, ontology: Dict[str, Any]):
        now = datetime.utcnow().isoformat()
        with self._connect() as connection:
            connection.execute(
                'UPDATE graphs SET ontology_json = ?, updated_at = ? WHERE graph_id = ?',
                (json.dumps(ontology, ensure_ascii=False), now, graph_id),
            )
            connection.commit()

    def get_ontology(self, graph_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT ontology_json FROM graphs WHERE graph_id = ?',
                (graph_id,),
            ).fetchone()
        if not row:
            return {}
        return self._loads_json(row['ontology_json'], {})

    def process_text_chunk(self, graph_id: str, chunk: str) -> str:
        ontology = self.get_ontology(graph_id)
        if not ontology:
            raise ValueError('图谱本体不存在，无法处理文本块')

        episode_uuid = str(uuid.uuid4())
        extraction = self._extract_graph_elements(chunk, ontology)
        self._persist_extraction(graph_id, extraction, episode_uuid)
        return episode_uuid

    def add_fact(
        self,
        graph_id: str,
        source_name: str,
        target_name: str,
        relation_name: str,
        fact: str,
        source_type: str = 'Agent',
        target_type: str = 'Activity',
        attributes: Optional[Dict[str, Any]] = None,
    ) -> str:
        """直接写入一条本地图谱事实，供模拟记忆更新等增量场景使用。"""
        if not self._graph_exists(graph_id):
            self.create_graph(graph_id, graph_id, 'Auto-created local graph')

        episode_uuid = str(uuid.uuid4())
        source_node = self._upsert_node(graph_id, {
            'name': source_name,
            'type': source_type,
            'summary': source_name,
            'attributes': {},
        })
        target_node = self._upsert_node(graph_id, {
            'name': target_name,
            'type': target_type,
            'summary': fact or target_name,
            'attributes': attributes or {},
        })
        self._upsert_edge(
            graph_id=graph_id,
            relationship={
                'name': relation_name,
                'fact': fact,
                'attributes': attributes or {},
            },
            source_node=source_node,
            target_node=target_node,
            episode_uuid=episode_uuid,
        )
        return episode_uuid

    def _graph_exists(self, graph_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT 1 FROM graphs WHERE graph_id = ? LIMIT 1',
                (graph_id,),
            ).fetchone()
        return row is not None

    def _extract_graph_elements(self, text: str, ontology: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
        entity_defs = [
            {
                'name': item.get('name', ''),
                'description': item.get('description', ''),
                'attributes': [attr.get('name', '') for attr in item.get('attributes', [])],
            }
            for item in ontology.get('entity_types', [])
        ]
        edge_defs = [
            {
                'name': item.get('name', ''),
                'description': item.get('description', ''),
                'source_targets': item.get('source_targets', []),
                'attributes': [attr.get('name', '') for attr in item.get('attributes', [])],
            }
            for item in ontology.get('edge_types', [])
        ]

        preferred_language = self._detect_preferred_language(text)
        language_rule = self._build_language_rule(preferred_language)

        system_prompt = (
            '你是一个知识图谱抽取器。请仅基于给定文本，提取实体和关系。'
            '必须返回有效 JSON，不要输出其他内容。'
        )
        user_prompt = f"""
允许的实体类型：
{json.dumps(entity_defs, ensure_ascii=False)}

允许的关系类型：
{json.dumps(edge_defs, ensure_ascii=False)}

请从下面文本中抽取信息：
{text}

返回 JSON：
{{
  "entities": [
    {{
      "name": "实体名称",
      "type": "实体类型名称",
      "summary": "一句话摘要",
      "attributes": {{"属性名": "属性值"}}
    }}
  ],
  "relationships": [
    {{
      "name": "关系类型名称",
      "fact": "自然语言事实描述",
      "source_entity_name": "源实体名称",
      "source_entity_type": "源实体类型",
      "target_entity_name": "目标实体名称",
      "target_entity_type": "目标实体类型",
      "attributes": {{"属性名": "属性值"}}
    }}
  ]
}}

约束：
1. 只能使用允许的实体类型和关系类型。
2. 没有明确提到的实体或关系不要编造。
3. 若文本信息不足，返回空数组。
4. attributes 中只保留字符串值。
5. {language_rule}
6. 同一现实对象只生成一个实体。不要把“美国”和“United States”、 “中国”和“China”拆成两个节点；若文本中同时出现中文名和英文别名，以文本主语言的名称作为实体 name，别名可写入 attributes.aliases。
"""

        try:
            response = self.llm.chat_json(
                messages=[
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_prompt},
                ],
                temperature=0.1,
                max_tokens=2500,
            )
        except Exception as exc:
            logger.warning(f'本地图谱抽取失败，返回空结果: {exc}')
            return {'entities': [], 'relationships': []}

        allowed_entity_types = {item['name'] for item in entity_defs if item['name']}
        allowed_edge_types = {item['name'] for item in edge_defs if item['name']}

        entities = []
        for item in response.get('entities', []):
            raw_name = str(item.get('name', '')).strip()
            name = self._canonicalize_entity_name(raw_name, preferred_language)
            entity_type = str(item.get('type', '')).strip()
            if not name or entity_type not in allowed_entity_types:
                continue
            attributes = self._normalize_attributes(item.get('attributes', {}))
            if raw_name and raw_name != name:
                attributes = self._append_alias(attributes, raw_name)
            entities.append({
                'name': name,
                'type': entity_type,
                'summary': str(item.get('summary', '')).strip() or name,
                'attributes': attributes,
            })

        entity_type_by_name = {self._normalize_name(item['name']): item['type'] for item in entities}

        relationships = []
        for item in response.get('relationships', []):
            relation_name = str(item.get('name', '')).strip()
            source_name = self._canonicalize_entity_name(
                str(item.get('source_entity_name', '')).strip(),
                preferred_language,
            )
            target_name = self._canonicalize_entity_name(
                str(item.get('target_entity_name', '')).strip(),
                preferred_language,
            )
            if not relation_name or relation_name not in allowed_edge_types:
                continue
            if not source_name or not target_name:
                continue

            source_type = str(item.get('source_entity_type', '')).strip() or entity_type_by_name.get(self._normalize_name(source_name), '')
            target_type = str(item.get('target_entity_type', '')).strip() or entity_type_by_name.get(self._normalize_name(target_name), '')
            if source_type and source_type not in allowed_entity_types:
                continue
            if target_type and target_type not in allowed_entity_types:
                continue

            relationships.append({
                'name': relation_name,
                'fact': str(item.get('fact', '')).strip() or f'{source_name} {relation_name} {target_name}',
                'source_entity_name': source_name,
                'source_entity_type': source_type,
                'target_entity_name': target_name,
                'target_entity_type': target_type,
                'attributes': self._normalize_attributes(item.get('attributes', {})),
            })

        return {'entities': entities, 'relationships': relationships}

    def _persist_extraction(
        self,
        graph_id: str,
        extraction: Dict[str, List[Dict[str, Any]]],
        episode_uuid: str,
    ):
        entities = extraction.get('entities', [])
        relationships = extraction.get('relationships', [])

        node_cache: Dict[str, Dict[str, Any]] = {}
        for entity in entities:
            node = self._upsert_node(graph_id, entity)
            node_cache[self._normalize_name(node['name'])] = node

        for relationship in relationships:
            source_node = node_cache.get(self._normalize_name(relationship['source_entity_name']))
            if source_node is None:
                source_node = self._find_or_create_node(
                    graph_id,
                    relationship['source_entity_name'],
                    relationship.get('source_entity_type', ''),
                )

            target_node = node_cache.get(self._normalize_name(relationship['target_entity_name']))
            if target_node is None:
                target_node = self._find_or_create_node(
                    graph_id,
                    relationship['target_entity_name'],
                    relationship.get('target_entity_type', ''),
                )

            if source_node is None or target_node is None:
                continue

            self._upsert_edge(
                graph_id=graph_id,
                relationship=relationship,
                source_node=source_node,
                target_node=target_node,
                episode_uuid=episode_uuid,
            )

    def _find_or_create_node(
        self,
        graph_id: str,
        name: str,
        entity_type: str,
    ) -> Optional[Dict[str, Any]]:
        existing = self.find_node_by_name(graph_id, name)
        if existing:
            return existing
        if not entity_type:
            return None
        return self._upsert_node(graph_id, {
            'name': name,
            'type': entity_type,
            'summary': name,
            'attributes': {},
        })

    def _upsert_node(self, graph_id: str, entity: Dict[str, Any]) -> Dict[str, Any]:
        now = datetime.utcnow().isoformat()
        entity = dict(entity)
        original_name = str(entity.get('name', '')).strip()
        preferred_language = self._detect_preferred_language(' '.join([
            original_name,
            str(entity.get('summary', '')),
            ' '.join(str(value) for value in self._normalize_attributes(entity.get('attributes', {})).values()),
        ]))
        entity['name'] = self._canonicalize_entity_name(original_name, preferred_language)
        normalized_name = self._normalize_name(entity['name'])
        existing = self.find_node_by_name(graph_id, entity['name'])
        labels = ['Entity']
        entity_type = str(entity.get('type', '')).strip()
        if entity_type:
            labels.append(entity_type)
        summary = str(entity.get('summary', '')).strip() or entity['name']
        attributes = self._normalize_attributes(entity.get('attributes', {}))
        if original_name and original_name != entity['name']:
            attributes = self._append_alias(attributes, original_name)

        if existing:
            merged_labels = sorted(set(existing['labels']) | set(labels))
            merged_attributes = existing['attributes']
            merged_attributes.update(attributes)
            merged_summary = summary if len(summary) >= len(existing['summary']) else existing['summary']
            with self._connect() as connection:
                connection.execute(
                    'UPDATE nodes SET labels_json = ?, summary = ?, attributes_json = ?, updated_at = ? WHERE uuid = ?',
                    (
                        json.dumps(merged_labels, ensure_ascii=False),
                        merged_summary,
                        json.dumps(merged_attributes, ensure_ascii=False),
                        now,
                        existing['uuid'],
                    ),
                )
                connection.commit()
            existing['labels'] = merged_labels
            existing['summary'] = merged_summary
            existing['attributes'] = merged_attributes
            existing['updated_at'] = now
            return existing

        node_uuid = str(uuid.uuid5(self._UUID_NAMESPACE, f'{graph_id}:node:{normalized_name}'))
        node = {
            'uuid': node_uuid,
            'name': entity['name'],
            'labels': sorted(set(labels)),
            'summary': summary,
            'attributes': attributes,
            'created_at': now,
            'updated_at': now,
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO nodes(uuid, graph_id, name, labels_json, summary, attributes_json, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_uuid,
                    graph_id,
                    node['name'],
                    json.dumps(node['labels'], ensure_ascii=False),
                    node['summary'],
                    json.dumps(node['attributes'], ensure_ascii=False),
                    now,
                    now,
                ),
            )
            connection.commit()
        return node

    def _upsert_edge(
        self,
        graph_id: str,
        relationship: Dict[str, Any],
        source_node: Dict[str, Any],
        target_node: Dict[str, Any],
        episode_uuid: str,
    ):
        now = datetime.utcnow().isoformat()
        signature = '|'.join([
            graph_id,
            relationship['name'],
            source_node['uuid'],
            target_node['uuid'],
            self._normalize_name(relationship['fact']),
        ])
        edge_uuid = str(uuid.uuid5(self._UUID_NAMESPACE, f'{signature}:edge'))
        existing = self.get_edge(edge_uuid)
        attributes = self._normalize_attributes(relationship.get('attributes', {}))
        episodes = [episode_uuid]

        if existing:
            merged_attributes = existing['attributes']
            merged_attributes.update(attributes)
            merged_episodes = list(dict.fromkeys(existing['episodes'] + episodes))
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE edges
                    SET attributes_json = ?, episodes_json = ?, updated_at = ?
                    WHERE uuid = ?
                    """,
                    (
                        json.dumps(merged_attributes, ensure_ascii=False),
                        json.dumps(merged_episodes, ensure_ascii=False),
                        now,
                        edge_uuid,
                    ),
                )
                connection.commit()
            return

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO edges(
                    uuid, graph_id, name, fact, fact_type,
                    source_node_uuid, target_node_uuid,
                    source_node_name, target_node_name,
                    attributes_json, created_at, episodes_json, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edge_uuid,
                    graph_id,
                    relationship['name'],
                    relationship['fact'],
                    relationship['name'],
                    source_node['uuid'],
                    target_node['uuid'],
                    source_node['name'],
                    target_node['name'],
                    json.dumps(attributes, ensure_ascii=False),
                    now,
                    json.dumps(episodes, ensure_ascii=False),
                    now,
                ),
            )
            connection.commit()

    def find_node_by_name(self, graph_id: str, name: str) -> Optional[Dict[str, Any]]:
        for candidate_name in self._candidate_entity_names(name):
            with self._connect() as connection:
                row = connection.execute(
                    'SELECT * FROM nodes WHERE graph_id = ? AND lower(name) = lower(?) LIMIT 1',
                    (graph_id, candidate_name),
                ).fetchone()
            if row:
                return self._row_to_node(row)
        return None

    def get_node(self, node_uuid: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT * FROM nodes WHERE uuid = ?',
                (node_uuid,),
            ).fetchone()
        return self._row_to_node(row) if row else None

    def get_edge(self, edge_uuid: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT * FROM edges WHERE uuid = ?',
                (edge_uuid,),
            ).fetchone()
        return self._row_to_edge(row) if row else None

    def get_all_nodes(
        self,
        graph_id: str,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query = 'SELECT * FROM nodes WHERE graph_id = ?'
        params: List[Any] = [graph_id]
        if cursor:
            query += ' AND uuid > ?'
            params.append(cursor)
        query += ' ORDER BY uuid ASC'
        if limit is not None:
            query += ' LIMIT ?'
            params.append(limit)

        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_node(row) for row in rows]

    def get_all_edges(
        self,
        graph_id: str,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query = 'SELECT * FROM edges WHERE graph_id = ?'
        params: List[Any] = [graph_id]
        if cursor:
            query += ' AND uuid > ?'
            params.append(cursor)
        query += ' ORDER BY uuid ASC'
        if limit is not None:
            query += ' LIMIT ?'
            params.append(limit)

        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_edge(row) for row in rows]

    def get_node_edges(self, node_uuid: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM edges
                WHERE source_node_uuid = ? OR target_node_uuid = ?
                ORDER BY uuid ASC
                """,
                (node_uuid, node_uuid),
            ).fetchall()
        return [self._row_to_edge(row) for row in rows]

    def get_graph_info(self, graph_id: str) -> Dict[str, Any]:
        nodes = self.get_all_nodes(graph_id)
        edges = self.get_all_edges(graph_id)
        entity_types = sorted({
            label
            for node in nodes
            for label in node['labels']
            if label not in ['Entity', 'Node']
        })
        return {
            'graph_id': graph_id,
            'node_count': len(nodes),
            'edge_count': len(edges),
            'entity_types': entity_types,
        }

    def get_graph_data(self, graph_id: str) -> Dict[str, Any]:
        nodes = self.get_all_nodes(graph_id)
        edges = self.get_all_edges(graph_id)
        return {
            'graph_id': graph_id,
            'nodes': nodes,
            'edges': edges,
            'node_count': len(nodes),
            'edge_count': len(edges),
        }

    def delete_graph(self, graph_id: str):
        with self._connect() as connection:
            connection.execute('DELETE FROM edges WHERE graph_id = ?', (graph_id,))
            connection.execute('DELETE FROM nodes WHERE graph_id = ?', (graph_id,))
            connection.execute('DELETE FROM graphs WHERE graph_id = ?', (graph_id,))
            connection.commit()

    def _row_to_node(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            'uuid': row['uuid'],
            'name': row['name'],
            'labels': self._loads_json(row['labels_json'], []),
            'summary': row['summary'] or '',
            'attributes': self._loads_json(row['attributes_json'], {}),
            'created_at': row['created_at'],
            'updated_at': row['updated_at'],
        }

    def _row_to_edge(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            'uuid': row['uuid'],
            'name': row['name'],
            'fact': row['fact'] or '',
            'fact_type': row['fact_type'] or row['name'] or '',
            'source_node_uuid': row['source_node_uuid'],
            'target_node_uuid': row['target_node_uuid'],
            'source_node_name': row['source_node_name'] or '',
            'target_node_name': row['target_node_name'] or '',
            'attributes': self._loads_json(row['attributes_json'], {}),
            'created_at': row['created_at'],
            'valid_at': row['valid_at'],
            'invalid_at': row['invalid_at'],
            'expired_at': row['expired_at'],
            'episodes': self._loads_json(row['episodes_json'], []),
        }

    @staticmethod
    def _normalize_name(value: str) -> str:
        return str(value or '').strip().lower()

    @classmethod
    def _detect_preferred_language(cls, text: str) -> str:
        return 'zh' if cls._contains_cjk(text) else 'source'

    @classmethod
    def _build_language_rule(cls, preferred_language: str) -> str:
        if preferred_language == 'zh':
            return (
                '输入文本以中文为主时，实体 name、summary、fact 和 attributes 的值必须使用中文或原文中已出现的中文表述；'
                '不要把中文实体翻译成英文，也不要额外生成英文同义节点。实体类型名称和关系类型名称仍必须使用上方允许列表中的 schema 名称。'
            )
        return (
            '实体 name、summary、fact 和 attributes 的值应保持原文主语言；不要为了补充别名而创建重复实体。'
        )

    @classmethod
    def _contains_cjk(cls, value: str) -> bool:
        return bool(cls._CJK_PATTERN.search(str(value or '')))

    @classmethod
    def _canonicalize_entity_name(cls, name: str, preferred_language: str = 'source') -> str:
        value = str(name or '').strip()
        if not value:
            return ''

        if preferred_language == 'zh':
            value = cls._prefer_chinese_from_bilingual_name(value)
            alias = cls._ZH_ALIAS_BY_KEY.get(cls._alias_key(value))
            if alias:
                return alias
        return value

    @classmethod
    def _prefer_chinese_from_bilingual_name(cls, value: str) -> str:
        value = str(value or '').strip()
        if not value:
            return ''
        parenthetical_cjk = re.search(r'[（(]\s*([\u4e00-\u9fff][^）)]*)\s*[）)]', value)
        if parenthetical_cjk and not cls._contains_cjk(value[:parenthetical_cjk.start()]):
            return parenthetical_cjk.group(1).strip()
        if cls._contains_cjk(value):
            stripped = cls._ASCII_PAREN_PATTERN.sub('', value).strip()
            return stripped or value
        return value

    @classmethod
    def _candidate_entity_names(cls, name: str) -> List[str]:
        raw_name = str(name or '').strip()
        candidates = [raw_name]
        zh_name = cls._canonicalize_entity_name(raw_name, 'zh')
        if zh_name and zh_name not in candidates:
            candidates.append(zh_name)
        return [candidate for candidate in candidates if candidate]

    @staticmethod
    def _alias_key(value: str) -> str:
        key = str(value or '').strip().lower()
        key = key.replace('’', "'")
        key = re.sub(r'\s+', ' ', key)
        key = re.sub(r'^the\s+', '', key)
        return key

    @staticmethod
    def _append_alias(attributes: Dict[str, str], alias: str) -> Dict[str, str]:
        cleaned_alias = str(alias or '').strip()
        if not cleaned_alias:
            return attributes
        existing = [item.strip() for item in attributes.get('aliases', '').split(';') if item.strip()]
        if cleaned_alias not in existing:
            existing.append(cleaned_alias)
        attributes['aliases'] = '; '.join(existing)
        return attributes

    @staticmethod
    def _normalize_attributes(attributes: Any) -> Dict[str, str]:
        if not isinstance(attributes, dict):
            return {}
        normalized: Dict[str, str] = {}
        for key, value in attributes.items():
            cleaned_key = str(key).strip()
            if not cleaned_key:
                continue
            cleaned_value = str(value).strip()
            if cleaned_value:
                normalized[cleaned_key] = cleaned_value
        return normalized

    @staticmethod
    def _loads_json(raw_value: Optional[str], fallback: Any):
        if not raw_value:
            return fallback
        try:
            return json.loads(raw_value)
        except json.JSONDecodeError:
            return fallback
