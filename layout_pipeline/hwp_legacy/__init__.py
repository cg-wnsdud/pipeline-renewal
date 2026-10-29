"""기존 parser-pipeline 의 HWP/HWPX 처리 경로를 그대로 옮긴 코드(docs/20).

출처: CGINSIDE-ROOKIES/nh-ad-compliance dev 186ddca(2026-10-29 pull), parser-pipeline/nh_parser_fin/ingest/
  hwp_render.py · hwp_html.py · hwp_layers.py · hwp_pagination.py · hwp_drawings.py(파일 그대로 복사, 수정하지 않음)
  hybrid.py = parser-pipeline/run.py 108-325행(구조 Region + 시각 블록 합치기 함수, 본문 그대로)

사내 `document-processor`(JPype/JVM) 는 함수 안에서 지연 import 한다. 변환(render_hwp_to_pdf)은 그 패키지가 설치된 가상환경에서
별도 프로세스로 실행하고(hwp_bridge, JVM 의 비ASCII 경로 문제로 ASCII 정션), DOM 행 → 구조 Region 변환(hwp_html.dom_rows_to_blocks)과
hybrid 함수는 순수 Python 이라 이 프로세스에서 쓴다(hwp_structure.py).
"""
SOURCE = {"repository": "CGINSIDE-ROOKIES/nh-ad-compliance", "branch": "dev", "commit": "186ddca",
          "files": ["parser-pipeline/nh_parser_fin/ingest/hwp_render.py", "parser-pipeline/nh_parser_fin/ingest/hwp_html.py",
                    "parser-pipeline/nh_parser_fin/ingest/hwp_layers.py", "parser-pipeline/nh_parser_fin/ingest/hwp_pagination.py",
                    "parser-pipeline/nh_parser_fin/ingest/hwp_drawings.py", "parser-pipeline/run.py#L108-L325"]}
