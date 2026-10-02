import os
import json # 1. json 라이브러리 추가
from pyvis.network import Network

def create_timeline_tree():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    report_dir = os.path.join(base_dir, "py_02_reports")
    os.makedirs(report_dir, exist_ok=True)
    
    net = Network(height="750px", width="100%", bgcolor="#ffffff", font_color="black", directed=True)

    # 2. 옵션 딕셔너리 설정
    options = {
        "layout": {
            "hierarchical": {
                "enabled": True,
                "direction": "LR",
                "sortMethod": "directed",
                "levelSeparation": 250,
                "nodeSpacing": 150
            }
        },
        "physics": {"enabled": False}
    }
    
    # 3. 핵심 수정 부분: json.dumps를 사용하여 올바른 JSON 문자열로 변환
    # 'var options = ...' 부분을 제거하고 순수 JSON만 전달합니다.
    net.set_options(json.dumps(options))

    # --- 이하 노드 및 엣지 추가 코드는 동일 ---
    net.add_node(1, label="2022: Original MZM Driver", level=0, color="#ffb3b3")
    net.add_node(2, label="2023: Improved PAM4 SerDes", level=1, color="#b3d9ff")
    net.add_node(3, label="2023: Optical Transceiver v1", level=1, color="#b3d9ff")
    net.add_node(4, label="2024: 112Gbps Low-power Driver", level=2, color="#b3ffb3")
    net.add_node(5, label="2024: Co-packaged Optics Study", level=2, color="#b3ffb3")

    net.add_edge(1, 2)
    net.add_edge(1, 3)
    net.add_edge(2, 4)
    net.add_edge(3, 4)
    net.add_edge(3, 5)

    output_path = os.path.join(report_dir, "timeline_tree.html")
    net.save_graph(output_path)
    print(f"✅ 타임라인 트리 생성 완료: {output_path}")

if __name__ == "__main__":
    create_timeline_tree()