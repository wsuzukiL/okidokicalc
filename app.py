import streamlit as st
import pandas as pd
import requests
import base64
import json
import re
import os
import datetime
import extra_streamlit_components as stx
from statistics import pstdev

# ==========================================
# Cookie Manager: スマホ用ブラウザ保存設定
# ==========================================
cookie_manager = stx.CookieManager()

# 環境変数やSecretsからの取得を優先
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
if not GOOGLE_API_KEY:
    try:
        GOOGLE_API_KEY = st.secrets.get("GOOGLE_API_KEY", "")
    except (FileNotFoundError, KeyError):
        pass

# セッションステートにAPIキーを保持する（再描画時の安定化のため）
if "api_key_state" not in st.session_state:
    st.session_state.api_key_state = ""

# クッキーから取得
cookie_val = cookie_manager.get(cookie="google_api_key")
if cookie_val and not GOOGLE_API_KEY:
    GOOGLE_API_KEY = cookie_val
    st.session_state.api_key_state = cookie_val

# 画面構成: タイトル
st.title("沖ドキ！GOLD 有利区間計算ツール")

# 初回APIキー入力UI
if not GOOGLE_API_KEY and not st.session_state.api_key_state:
    st.warning("⚠️ Google Cloud Vision APIキーが設定されていません。")
    st.info("初回のみ、以下の枠にAPIキーを入力してください。お使いのスマホ(ブラウザ)に安全に保存され、次回からは入力を省略できます。")
    
    with st.form(key="api_form"):
        api_key_input = st.text_input("🔑 APIキー", type="password")
        submit_button = st.form_submit_button(label="キーを保存して開始する")
        
        if submit_button:
            if api_key_input:
                st.session_state.api_key_state = api_key_input
                cookie_manager.set("google_api_key", api_key_input, expires_at=datetime.datetime(2030, 1, 1))
                st.success("APIキーを保存しました！画面をリロードしています...")
                # Streamlitのライフサイクル都合上、少し間を置いてリランするような挙動にする
                import time
                time.sleep(1)
                st.rerun()
            else:
                st.error("APIキーを入力してください。")
    st.stop()  # キーがない場合はここで画面描画を停止

# キーが確定した場合、状態に同期
if GOOGLE_API_KEY:
    st.session_state.api_key_state = GOOGLE_API_KEY

# サイドバーにリセットボタン配置
with st.sidebar:
    st.header("⚙️ 設定")
    if st.button("🔄 APIキーの保存をリセット"):
        cookie_manager.delete("google_api_key")
        st.session_state.api_key_state = ""
        import time
        time.sleep(0.5)
        st.rerun()

# ==========================================
# OCR 処理関数
# ==========================================
def analyze_image_with_vision_api(image_bytes, api_key):
    url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    payload = {
        "requests": [
            {
                "image": {"content": base64_image},
                "features": [{"type": "DOCUMENT_TEXT_DETECTION"}]
            }
        ]
    }
    headers = {"Content-Type": "application/json"}
    response = requests.post(url, data=json.dumps(payload), headers=headers)
    result = response.json()
    
    if "error" in result:
        st.error(f"APIエラー: {result['error']['message']}")
        return None
        
    try:
        return result["responses"][0]["textAnnotations"]
    except KeyError:
        return None

def parse_ocr_text(annotations):
    """
    データカウンタ特有の座標クラスタリングを用いた高精度パーサー。
    ノイズを取り除き、履歴と「現在のゲーム数」を両方抽出する。
    """
    if not annotations or len(annotations) < 2:
        return [], 0
        
    words = annotations[1:]
    
    # 1. 座標つきアイテムリストを作成
    items = []
    for w in words:
        text = w.get("description", "").strip()
        poly = w.get("boundingPoly", {}).get("vertices", [])
        if len(poly) == 4 and text:
            y_centers = [v.get("y", 0) for v in poly]
            x_centers = [v.get("x", 0) for v in poly]
            
            items.append({
                "text": text,
                "x": sum(x_centers) / 4.0,
                "y": sum(y_centers) / 4.0,
                "width": max(x_centers) - min(x_centers),
                "height": max(y_centers) - min(y_centers)
            })
            
    if not items:
        return [], 0

    # 2. 種類(BB/RB)と数字を抽出
    bb_rb_pattern = re.compile(r'\b(BB|RB|BIG|REG|8B|R8)\b', re.IGNORECASE)
    number_pattern = re.compile(r'^(\d{1,4})(?:G|G\s*)?$')
    
    types = []
    nums = []
    
    for item in items:
        text = item["text"].upper()
        if bb_rb_pattern.match(text) or "BIG" in text or "REG" in text or "BB" in text or "RB" in text:
            t = "BB" if any(x in text for x in ["BIG", "BB", "8B"]) else "RB"
            types.append({**item, "val": t})
            
        elif number_pattern.match(text) or text.isdigit():
            val = int(re.sub(r'[^0-9]', '', text))
            if 0 < val <= 2000:
                nums.append({**item, "val": val})
                
    if not nums:
        return [], 0

    # 3. 現在のゲーム数(ハマりG数)を推定する
    current_game = 0
    used_nums = set()
    
    keyword_items = [item for item in items if any(k in item["text"] for k in ["現在", "スタート", "回転", "ハマ", "G数"])]
    
    if keyword_items:
        target_keyword = keyword_items[0]
        # Y座標がキーワードより大きく上ではない数字（通常は下や右にある）
        possible_nums = [n for n in nums if n["y"] > target_keyword["y"] - 20]
        if possible_nums:
            possible_nums.sort(key=lambda n: ((n["x"] - target_keyword["x"])**2 + (n["y"] - target_keyword["y"])**2))
            current_game = possible_nums[0]["val"]
            used_nums.add(id(possible_nums[0]))
            
    if current_game == 0 and nums:
        # 文字面積が最大のものが現在ゲーム数である可能性が高い
        biggest_num = max(nums, key=lambda n: n["width"] * n["height"])
        avg_area = sum(n["width"] * n["height"] for n in nums) / len(nums)
        if (biggest_num["width"] * biggest_num["height"]) > avg_area * 1.5:
            current_game = biggest_num["val"]
            used_nums.add(id(biggest_num))

    history_nums = [n for n in nums if id(n) not in used_nums]

    if not types or not history_nums:
        return [], current_game

    # 4. リストが縦並びか横並びかを判定
    types_x = [t["x"] for t in types]
    types_y = [t["y"] for t in types]
    
    std_x = pstdev(types_x) if len(types_x) > 1 else 0
    std_y = pstdev(types_y) if len(types_y) > 1 else 0
    
    is_vertical_layout = std_x <= std_y  # X座標のブレが少ない＝縦並び
    
    # 5. 数字をクラスタ(列または行)にグループ化
    res = []
    clusters = []
    if is_vertical_layout:
        history_nums.sort(key=lambda n: n["x"])
        if history_nums:
            current_cluster = [history_nums[0]]
            for n in history_nums[1:]:
                if abs(n["x"] - current_cluster[-1]["x"]) <= max(40, n["width"]):
                    current_cluster.append(n)
                else:
                    clusters.append(current_cluster)
                    current_cluster = [n]
            clusters.append(current_cluster)
            
        if clusters:
            target_len = len(types)
            best_cluster = min(clusters, key=lambda c: abs(len(c) - target_len))
            types.sort(key=lambda t: t["y"])
            best_cluster.sort(key=lambda n: n["y"])
            
            for i in range(min(len(types), len(best_cluster))):
                res.append({"BR": types[i]["val"], "ゲーム数": best_cluster[i]["val"]})
            
    else:
        history_nums.sort(key=lambda n: n["y"])
        if history_nums:
            current_cluster = [history_nums[0]]
            for n in history_nums[1:]:
                if abs(n["y"] - current_cluster[-1]["y"]) <= max(20, n["height"]):
                    current_cluster.append(n)
                else:
                    clusters.append(current_cluster)
                    current_cluster = [n]
            clusters.append(current_cluster)
            
        if clusters:
            target_len = len(types)
            best_cluster = min(clusters, key=lambda c: abs(len(c) - target_len))
            types.sort(key=lambda t: t["x"])
            best_cluster.sort(key=lambda n: n["x"])
            
            for i in range(min(len(types), len(best_cluster))):
                res.append({"BR": types[i]["val"], "ゲーム数": best_cluster[i]["val"]})

    return res, current_game

# ==========================================
# UI 構築 
# ==========================================
if "history_data" not in st.session_state:
    st.session_state.history_data = pd.DataFrame(
        [
            {"BR": "BB", "ゲーム数": 120},
            {"BR": "RB", "ゲーム数": 15},
            {"BR": "BB", "ゲーム数": 10},
            {"BR": "BB", "ゲーム数": 400},
            {"BR": "BB", "ゲーム数": 5},
            {"BR": "RB", "ゲーム数": 200},
        ]
    )

if "current_game_state" not in st.session_state:
    st.session_state.current_game_state = 0

st.markdown("### 画像アップロード (OCR用)")
uploaded_file = st.file_uploader("データカウンタの履歴画像をアップロードしてください", type=["jpg", "jpeg", "png"])

if uploaded_file is not None:
    if st.button("🔍 画像から履歴を読み取る"):
        with st.spinner("画像を解析中..."):
            image_bytes = uploaded_file.getvalue()
            annotations = analyze_image_with_vision_api(image_bytes, GOOGLE_API_KEY)
            
            if annotations is not None and len(annotations) > 0:
                st.success("テキスト情報を抽出し、不要な数値を自動除外しました！")
                
                parsed_history, current_game = parse_ocr_text(annotations)
                
                if parsed_history or current_game > 0:
                    if parsed_history:
                        st.session_state.history_data = pd.DataFrame(parsed_history)
                    if current_game > 0:
                        st.session_state.current_game_state = current_game
                    st.info(f"履歴テーブルを更新し、現在のゲーム数を {current_game}G に設定しました。誤りがあれば修正してください。")
                    st.rerun()
                else:
                    st.warning("画像から履歴データ(列・行)や現在のゲーム数を正しく認識できませんでした。")

st.divider()

# ==========================================
# カスタムCSSの注入
# ==========================================
st.markdown("""
<style>
.badge-big {
    background-color: #dc3545;
    color: white;
    padding: 3px 10px;
    border-radius: 6px;
    font-weight: bold;
    font-size: 0.85em;
    display: inline-block;
    width: 45px;
    text-align: center;
}
.badge-reg {
    background-color: #007bff;
    color: white;
    padding: 3px 10px;
    border-radius: 6px;
    font-weight: bold;
    font-size: 0.85em;
    display: inline-block;
    width: 45px;
    text-align: center;
}
.badge-now {
    background-color: #ffc107;
    color: black;
    padding: 3px 10px;
    border-radius: 6px;
    font-weight: bold;
    font-size: 0.85em;
    display: inline-block;
    width: 45px;
    text-align: center;
}
.history-row {
    font-size: 1.05em;
    padding: 10px 5px;
    border-bottom: 1px solid #f0f0f0;
    display: flex;
    align-items: center;
    justify-content: space-between;
}
.history-col-num {
    width: 55px;
    color: #555;
    font-size: 0.9em;
}
.history-col-game {
    width: 50px;
    text-align: right;
    font-weight: bold;
}
.history-col-cum {
    color: #666;
    font-size: 0.85em;
    text-align: right;
    flex-grow: 1;
}
.history-container {
    background-color: #ffffff;
    padding: 15px;
    border-radius: 10px;
    box-shadow: 0 4px 6px rgba(0,0,0,0.05);
    margin-bottom: 20px;
    border: 1px solid #e6e6e6;
}
.cut-row {
    background-color: #f8f9fa;
    opacity: 0.6;
}
</style>
""", unsafe_allow_html=True)

# ==========================================
# 手動入力・修正エリア
# ==========================================
st.markdown("### ⚙️ 履歴の手動修正・追加")
st.caption("※ 一番上が「最新の履歴」になるように入力してください。")

def style_br_column(val):
    if val == "BB":
        return "color: #dc3545; font-weight: bold;"
    elif val == "RB":
        return "color: #007bff; font-weight: bold;"
    return ""

styled_df = st.session_state.history_data.style.map(style_br_column, subset=["BR"])

edited_df = st.data_editor(
    styled_df,
    num_rows="dynamic",
    use_container_width=True,
    column_config={
        "BR": st.column_config.SelectboxColumn("BR", options=["BB", "RB"], required=True),
        "ゲーム数": st.column_config.NumberColumn("ゲーム数", min_value=1, step=1)
    },
    column_order=["BR", "ゲーム数"]
)
current_game = st.number_input("現在のゲーム数（ハマりG数）", min_value=0, step=1, key="current_game_state")

# ==========================================
# 自動計算ロジック & 美しいリストUI描画
# ==========================================
history = edited_df.to_dict("records")

if not history and current_game == 0:
    st.info("画像を読み取るか、手動で履歴を入力してください。")
else:
    history_reversed = list(reversed(history))
    
    # 有利区間リセット地点(32G以内の連チャン抜け後)を探す
    origin_idx = 0
    prev_was_chain = False
    
    for i, row in enumerate(history_reversed):
        try:
            g = int(row.get("ゲーム数", 0))
        except (ValueError, TypeError):
            g = 0
            
        if g <= 32:
            prev_was_chain = True
        else:
            if prev_was_chain:
                origin_idx = i
            prev_was_chain = False

    # HTMLレンダリングと計算を同時に実行
    html_out = ["<div class='history-container'>"]
    
    total_games = 0
    display_count = 1
    
    for i, row in enumerate(history_reversed):
        try:
            g = int(row.get("ゲーム数", 0))
        except (ValueError, TypeError):
            g = 0
        b_type = row.get("BR", "BB")
        
        is_cut = i < origin_idx
        row_class = "history-row cut-row" if is_cut else "history-row"
        
        if is_cut:
            html_out.append(f"""
            <div class='{row_class}'>
                <span class='history-col-num'>--回目</span>
                <span class='history-col-game'>{g}G</span>
                <span class='badge-{'big' if b_type=='BB' else 'reg'}'>{'BIG' if b_type=='BB' else 'REG'}</span>
                <span class='history-col-cum'>連チャン中 (除外)</span>
            </div>
            """)
        else:
            start_g = total_games + g
            if b_type == "BB":
                end_g = start_g + 69
                badge = "<span class='badge-big'>BIG</span>"
            else:
                end_g = start_g + 29
                badge = "<span class='badge-reg'>REG</span>"
                
            html_out.append(f"""
            <div class='{row_class}'>
                <span class='history-col-num'>{display_count}回目</span>
                <span class='history-col-game'>{g}G</span>
                {badge}
                <span class='history-col-cum'>{start_g}G &rarr; {end_g}G</span>
            </div>
            """)
            total_games = end_g
            display_count += 1
            
    # 現在のゲーム数行
    if current_game > 0 or total_games > 0:
        html_out.append(f"""
        <div class='history-row' style='background-color: #fffdf5;'>
            <span class='history-col-num'>現在</span>
            <span class='history-col-game'>{current_game}G</span>
            <span class='badge-now'>現在G</span>
            <span class='history-col-cum' style='color: #000; font-weight: bold;'>{total_games + current_game}G</span>
        </div>
        """)
        
    html_out.append("</div>")
    
    # (計算用変数の更新のみ残し、HTMLの構築箇所は不要だがロジック維持のためそのまま)
    total_games += current_game
    remaining_games = max(0, 2000 - total_games)
    
    st.markdown("## 🎯 最終結果")
    
    col1, col2 = st.columns(2)
    with col1:
        st.metric(label="累計消化ゲーム数", value=f"{total_games} G")
    with col2:
        st.metric(label="2000Gまでの残り", value=f"{remaining_games} G")

    if remaining_games == 0:
        st.success("🎉 すでに有利区間天井(2000G)に到達している可能性があります！")
    elif remaining_games <= 500:
        st.warning("🔥 天井まであと少しです！")
    else:
        st.info("まだまだ天井までは距離があります。")
