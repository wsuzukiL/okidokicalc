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
@st.cache_resource(experimental_allow_widgets=True)
def get_manager():
    return stx.CookieManager()

cookie_manager = get_manager()

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
if not GOOGLE_API_KEY:
    try:
        GOOGLE_API_KEY = st.secrets.get("GOOGLE_API_KEY", "")
    except (FileNotFoundError, KeyError):
        pass

if not GOOGLE_API_KEY:
    # クッキーから取得 (モバイル等で保存した場合)
    val = cookie_manager.get(cookie="google_api_key")
    if val:
        GOOGLE_API_KEY = val

# 画面構成: タイトル
st.title("沖ドキ！GOLD 有利区間計算ツール")

# 初回APIキー入力UI
if not GOOGLE_API_KEY:
    st.warning("⚠️ Google Cloud Vision APIキーが設定されていません。")
    st.info("初回のみ、以下の枠にAPIキーを入力してください。お使いのスマホ(ブラウザ)に安全に保存され、次回からは入力を省略できます。")
    api_key_input = st.text_input("🔑 APIキー", type="password")
    if st.button("キーを保存して開始する", type="primary"):
        if api_key_input:
            cookie_manager.set("google_api_key", api_key_input, expires_at=datetime.datetime(2030, 1, 1))
            st.success("APIキーを保存しました！ページをリロードしてください。")
            st.rerun()
        else:
            st.error("APIキーを入力してください。")
    st.stop()  # キーがない場合はここで画面描画を停止

# サイドバーにリセットボタン配置
with st.sidebar:
    st.header("⚙️ 設定")
    if st.button("🔄 APIキーの保存をリセット"):
        cookie_manager.delete("google_api_key")
        st.success("APIキーを削除しました。リロードしてください。")
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
    ノイズ（総回転数やグラフの縦軸数値、確率など）を座標の縦横の並び(列・行)から判定して完全に除外する。
    """
    if not annotations or len(annotations) < 2:
        return []
        
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
        return []

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
            # 沖ドキのゲーム数にあり得る数字のみ (1〜2000)
            if 0 < val <= 2000:
                nums.append({**item, "val": val})
                
    if not types or not nums:
        return []

    # 3. リストが縦並びか横並びかを判定 (標準偏差でチェック)
    types_x = [t["x"] for t in types]
    types_y = [t["y"] for t in types]
    
    std_x = pstdev(types_x) if len(types_x) > 1 else 0
    std_y = pstdev(types_y) if len(types_y) > 1 else 0
    
    is_vertical_layout = std_x <= std_y  # X座標のブレが少ない＝縦一列に並んでいる
    
    # 4. 数字をクラスタ(列または行)にグループ化し、無関係な数字の列を除外する
    clusters = []
    if is_vertical_layout:
        # 縦並び：X座標が近いものを同じ列とする
        nums.sort(key=lambda n: n["x"])
        if nums:
            current_cluster = [nums[0]]
            for n in nums[1:]:
                # X座標が近い(例: 幅の範囲内か40px以内)なら同じ列
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
            
            res = []
            for i in range(min(len(types), len(best_cluster))):
                res.append({"ゲーム数": best_cluster[i]["val"], "種類": types[i]["val"]})
            return res
            
    else:
        # 横並び：Y座標が近いものを同じ行とする
        nums.sort(key=lambda n: n["y"])
        if nums:
            current_cluster = [nums[0]]
            for n in nums[1:]:
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
            
            res = []
            for i in range(min(len(types), len(best_cluster))):
                res.append({"ゲーム数": best_cluster[i]["val"], "種類": types[i]["val"]})
            return res

    return []

# ==========================================
# UI 構築 
# ==========================================
if "history_data" not in st.session_state:
    st.session_state.history_data = pd.DataFrame(
        [
            {"ゲーム数": 120, "種類": "BB"},
            {"ゲーム数": 15, "種類": "RB"},
            {"ゲーム数": 10, "種類": "BB"},
            {"ゲーム数": 400, "種類": "BB"},
            {"ゲーム数": 5, "種類": "BB"},
            {"ゲーム数": 200, "種類": "RB"},
        ]
    )

st.markdown("### 画像アップロード (OCR用)")
uploaded_file = st.file_uploader("データカウンタの履歴画像をアップロードしてください", type=["jpg", "jpeg", "png"])

if uploaded_file is not None:
    if st.button("🔍 画像から履歴を読み取る"):
        with st.spinner("画像を解析中..."):
            image_bytes = uploaded_file.getvalue()
            annotations = analyze_image_with_vision_api(image_bytes, GOOGLE_API_KEY)
            
            if annotations is not None and len(annotations) > 0:
                st.success("テキスト情報を抽出し、不要な数値を自動除外しました！")
                
                parsed_history = parse_ocr_text(annotations)
                
                if parsed_history:
                    st.session_state.history_data = pd.DataFrame(parsed_history)
                    st.info("履歴テーブルを自動更新しました。誤りがあれば修正してください。")
                    st.rerun()
                else:
                    st.warning("画像から履歴データ(列・行)を正しく認識できませんでした。")

st.divider()

st.markdown("### ボーナス履歴")
st.caption("※上から「最新の履歴」になるように入力してください。現在のハマりゲーム数は下の入力欄に入力します。")

edited_df = st.data_editor(
    st.session_state.history_data,
    num_rows="dynamic",
    use_container_width=True,
    column_config={
        "ゲーム数": st.column_config.NumberColumn("ゲーム数", min_value=1, step=1),
        "種類": st.column_config.SelectboxColumn("種類", options=["BB", "RB"], required=True)
    }
)

current_game = st.number_input("現在のゲーム数（ハマりG数）", min_value=0, value=0, step=1)

if st.button("計算する", type="primary"):
    history = edited_df.to_dict("records")
    
    if not history:
        st.warning("履歴データを一つ以上入力してください。")
    else:
        history_reversed = list(reversed(history))
        
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
                
        total_games = 0
        for i in range(origin_idx, len(history_reversed)):
            row = history_reversed[i]
            try:
                g = int(row.get("ゲーム数", 0))
            except (ValueError, TypeError):
                g = 0
            b_type = row.get("種類", "BB")
            
            total_games += g
            if b_type == "BB":
                total_games += 69
            elif b_type == "RB":
                total_games += 29
                
        total_games += current_game
        remaining_games = max(0, 2000 - total_games)
        
        st.divider()
        st.markdown("## 🎯 計算結果")
        
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
