# MWeft

[English](README.md) | **한국어**

> MCP를 위한 로컬 우선(local-first) 그래프 메모리 — **이벤트**가 곧 엣지인 구조.

MWeft는 단일 SQLite 파일 하나로 동작하는 stdio MCP 서버로 제공되는 임베디드
지식그래프 메모리 레이어입니다. 프로비저닝할 서버도, 필수 원격 API 키도,
설치할 그래프 DB도 없습니다 — 파이썬 프로세스와 파일 하나면 됩니다.

개별 사실(fact)이 아니라 **관계의 뉘앙스**에 가치가 있는 메모리를 위해
설계됐습니다.

## 무엇이 다른가

대부분의 그래프 메모리 시스템(Graphiti, mem0 등)은 에피소드를 적재하면서
거기서 **타입이 지정된 엔티티-엔티티 관계**(`A ─[KNOWS]─ B`,
`A ─[WORKS_AT]─ C`)를 추출합니다. 이 추출은 쓰기 시점에 모델의 해석을
확정하고, 관계의 뉘앙스를 이산 라벨로 양자화하며, 환각(hallucination)
표면을 하나 더 만듭니다.

**MWeft는 반대 입장을 취합니다: 이벤트 자체가 엣지입니다.** 두 엔티티는
같은 이벤트에 함께 참여하면 "관련"되며, 관계의 내용은 그 이벤트의 벡터 +
요약에 담깁니다. 타입 관계 추출 단계도, 유지할 관계 스키마도, 적재 시
양자화도 없습니다.

| | MWeft | Graphiti / mem0 |
|---|---|---|
| 관계 모델 | event = edge (벡터 + 요약) | 타입 추출 엣지 |
| 뉘앙스 보존 | 완전 (연속 임베딩) | 손실 (이산 라벨) |
| 스키마 / 드리프트 | 없음 | 온톨로지 필요 |
| 멀티홉 추론 | 의미 기반 (soft-typed) | 정밀 (hard-typed) |
| "아직 유효한가?" | 질의 시점 합성 | 엣지 무효화 |
| 적합 영역 | 서사·설계·진화하는 의미 | 사실형 KB, 시간에 따라 변하는 사실 |

MWeft 모델은 질의 시점에 타입 검색을 *근사*할 수 있습니다(soft-typed 의미
홉). 그 반대 — 이미 라벨이 붙은 엣지에서 뉘앙스를 복원하는 것 — 은
불가능합니다. 그래서 MWeft는 관계가 이산적이기보다 결(texture)을 지닐 때
적합합니다: 글쓰기, 설계 맥락, 근거가 담긴 프로젝트 이력 등 "무엇"만큼
"왜"가 중요한 모든 것.

## 30초 아키텍처

세 가지 노드 종류 — **엔티티(entities)**, **이벤트(events)**,
**태그(tags)** — 와 소수의 엣지:

- `participated_in` (엔티티 ↔ 이벤트)
- `event_sequential_next` (이벤트 → 이벤트 — 문서 순서 / 스레드)
- `event_member_of` (이벤트 → 태그)
- `entity_connection` (엔티티 ↔ 엔티티 — 동시출현 횟수)
- `event_jaccard_connected` (이벤트 ↔ 이벤트 — 공유 엔티티/태그 풋프린트)

엔티티와 이벤트 모두 벡터 임베딩(기본 BGE-M3, 프로세스 내 임베딩)을
갖습니다. 검색은 히트와 함께 **연결 맵 힌트(connection-map hint)** 를
반환합니다 — 인접 이벤트, 공유 엔티티, 동시출현 이웃, 의미적으로 유사한
이벤트를 추가 도구 호출 없이 LLM에게 가리켜 주는 구조입니다.

원시 그래프 위에서 MWeft는 엔티티 그래프와 이벤트 그래프 양쪽에 **Leiden
커뮤니티 탐지**를 돌려 수작업 라벨링 없이 떠오르는 클러스터("auto-tags")를
만들어 냅니다. `mweft_auto_tag_*` 와 `mweft_community_*` 도구로 LLM이
클러스터 구조를 요약하고 특정 커뮤니티의 멤버로 파고들 수 있습니다.

전체 그래프는 하나의 SQLite 파일에 저장됩니다(벡터 인덱스는 `sqlite-vec`).
대안 백엔드로 Postgres + pgvector를 지원합니다.

## 이 배포본에 포함된 것

MCP 표면은 엄선된 읽기/저장 도구를 노출합니다:

| | |
|---|---|
| **검색 / 읽기** | `mweft_search`, `mweft_entity_lookup`, `mweft_get_event_content` |
| **그래프 탐색** | `mweft_neighbors`, `mweft_relations`, `mweft_temporal_flow` |
| **커뮤니티 / auto-tags** | `mweft_auto_tag_summarize`, `mweft_auto_tag_list`, `mweft_auto_tag_members`, `mweft_auto_tag_detail`, `mweft_auto_tag_of`, `mweft_community_explore`, `mweft_community_residual` |
| **쓰기** | `mweft_remember`, `mweft_remember_edit` |
| **자유 SQL** | `mweft_sql_query`, `mweft_describe_schema`, `mweft_explain_query` |

힌트 표면(모든 검색에 함께 반환):

- `connections.sequential` — 인접 문서 청크
- `connections.similar` — 엔티티 임베딩 이웃
- `connections.connected` — 동시출현 이웃
- `hint.threads` / `entities` / `context_groups` / `categories` — 고정된
  `reason` 태그가 붙은 히트 간 연결 맵

## 상태

**프리릴리스(Pre-release).** 핵심 메모리 모델과 검색 표면은 안정적입니다.

이 배포본에 (아직) 없는 것:
- 멀티테넌트 공유(`share_groups`, RLS 기반 접근 제어).
- 전체 K2G 빌드 파이프라인(무거운 LLM 적재). MWeft는 `mweft_remember`로
  메모리를 받습니다; 대규모 빌드는 별개 사안입니다.

## 설치

권장 구성은 로컬 **ONNX** 임베딩입니다 — API 키 불필요, 런타임에 PyTorch
불필요:

```bash
pip install -e .[embed-onnx]
```

BGE-M3 모델을 ONNX로 한 번 export 하고
(`optimum-cli export onnx -m BAAI/bge-m3 ./models/bge-m3-onnx`)
`EMBEDDING_ONNX_PATH`로 가리키세요. 키 한 줄로 끝나는 API 방식이나 PyTorch
관리 가중치가 더 편하다면 [install.md](install.md#embedding--onnx-openai-or-pytorch)의
임베딩 옵션을 참고하세요.

그다음 MCP 클라이언트에 서버를 등록합니다 — 모든 설정은 클라이언트의
`env` 블록에 들어가며 별도 설정 파일이 없습니다. Claude Code 예시
(`~/.claude.json` 또는 프로젝트 단위 `.mcp.json`):

```json
{
  "mcpServers": {
    "mweft": {
      "command": "k2g-mcp",
      "env": {
        "DATA_DIR": "/absolute/path/to/mweft_data",
        "EMBEDDING_PROVIDER": "onnx",
        "EMBEDDING_ONNX_PATH": "/absolute/path/to/models/bge-m3-onnx",
        "EMBEDDING_DIM": "1024"
      }
    }
  }
}
```

전체 설정 — 클라이언트별(Claude Code / Desktop, Cursor, Gemini CLI),
SQLite vs PostgreSQL 백엔드, 모든 env 항목 — 은 [install.md](install.md)를
참고하세요.

선택적 네이티브 데스크탑 앱(`mweft-app`)은 도메인·태그·엔티티·검색을
둘러볼 수 있는 창을 제공합니다. `manager` extra
(`pip install -e .[manager]`)를 설치하고
[install.md](install.md#manager-desktop-app-mweft-app)를 참고하세요.

LLM이 MWeft를 잘 쓰도록 만드는 방법(검색 휴리스틱, 저장 트리거, 연결 맵
힌트)은 [prompt_guide.md](prompt_guide.md)를 참고하세요 — 스니펫을
`CLAUDE.md` / `GEMINI.md` / `.cursorrules`에 넣으면 됩니다.

## 첫 실행 & 백신

맨 처음 실행 — 또는 재부팅 직후 첫 연결 — 은 느릴 수 있습니다. 백신/보안
소프트웨어(Windows Defender 등)가 임베딩 모델, 런타임 라이브러리, DB 파일을
**첫 접근 시** 검사하기 때문이며, 한 번 검사되면 시작이 빨라집니다. 매번
콜드 부팅이 느리다면 **설치 폴더와 `DATA_DIR`을 백신 실시간 검사 예외에
추가**하면 콜드 스타트가 크게 빨라집니다. macOS에서는 Gatekeeper / 격리
검사가 비슷한 일회성 첫 실행 지연을 유발할 수 있습니다. 이는 멈춤이 아니라
검사 비용입니다 — 서버는 정상적으로 뜨며, 첫 콜드 접근만 느립니다.

## 라이선스

[Apache License 2.0](LICENSE).

---

**ECKG 맥락.** MWeft는 2024–2026년에 주목받은 *이벤트 중심 지식그래프
(event-centric knowledge graph, ECKG)* 영역에 속합니다. 대부분의 ECKG
연구가 *추출 품질*에 집중하는 반면, MWeft는 관계가 결을 지닐 때 추출이
과연 옳은 선택인지를 묻습니다.
