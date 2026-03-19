import streamlit as st
import pandas as pd
import requests
import base64
import json
import re
import os
import datetime
from statistics import pstdev

# ==========================================
# APIキー取得 (Secrets / Enum 専用)
# ==========================================
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
if not GOOGLE_API_KEY:
    try:
        GOOGLE_API_KEY = st.secrets.get("GOOGLE_API_KEY", "")
    except (FileNotFoundError, KeyError):
        pass

if not GOOGLE_API_KEY:
    st.error("【要設定】Google Cloud Vision APIキーが設定されていません。Streamlit Cloudの Secrets に `GOOGLE_API_KEY` を設定してください。")
    st.stop()

# ==========================================
# スマホ特化レイアウト調整
# ==========================================
st.markdown("""
<style>
    /* スマホ画面向けの極小パディング */
    .block-container {
        padding-top: 1rem;
        padding-bottom: 2rem;
        padding-left: 0.5rem;
        padding-right: 0.5rem;
        max-width: 100%;
    }
    /* ヘッダー・フッター非表示 */
    header {visibility: hidden;}
    /* ファイルアップローダーの小型化 */
    [data-testid="stFileUploaderDropzone"] {
        padding: 10px !important;
    }
    [data-testid="stFileUploaderDropzone"] svg {
        width: 30px !important;
        height: 30px !important;
        margin-bottom: 5px !important;
    }
    [data-testid="stFileUploaderDropzone"] div {
        font-size: 0.8rem !important;
    }
    [data-testid="stFileUploaderDropzone"] small {
        font-size: 0.6rem !important;
    }
    /* アップローダーのラベル（「履歴画像をアップロード」）も小さく */
    [data-testid="stFileUploader"] label p {
        font-size: 0.8rem !important;
    }
</style>
""", unsafe_allow_html=True)

st.markdown("<h3 style='text-align:center; font-size:1.4rem; margin-top:-10px; margin-bottom:-10px;'>沖ドキGOLDチェッカー</h3>", unsafe_allow_html=True)

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
            t = "🔴 BIG" if any(x in text for x in ["BIG", "BB", "8B"]) else "🔵 REG"
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
            {"BR": "🟡 現在G", "ゲーム数": 32},
            {"BR": "🔴 BIG", "ゲーム数": 120},
            {"BR": "🔵 REG", "ゲーム数": 15},
            {"BR": "🔴 BIG", "ゲーム数": 10},
            {"BR": "🔴 BIG", "ゲーム数": 400},
            {"BR": "🔴 BIG", "ゲーム数": 5},
            {"BR": "🔵 REG", "ゲーム数": 200},
        ]
    )

if "force_origin_idx" not in st.session_state:
    st.session_state.force_origin_idx = None

st.markdown("<div style='font-size:1.0rem; font-weight:bold; margin-bottom:-10px; margin-top: 10px;'>画像アップロード</div>", unsafe_allow_html=True)
uploaded_file = st.file_uploader("履歴画像をアップロード", type=["jpg", "jpeg", "png"])

if uploaded_file is not None:
    # プレビュー画像を小さくサムネイル表示
    st.image(uploaded_file, width=250)
        
    if st.button("🔍 画像から履歴を読み取る"):
        with st.spinner("画像を解析中..."):
            image_bytes = uploaded_file.getvalue()
            annotations = analyze_image_with_vision_api(image_bytes, GOOGLE_API_KEY)
            
            if annotations is not None and len(annotations) > 0:
                st.success("テキスト情報を抽出し、不要な数値を自動除外しました！")
                
                parsed_history, current_game = parse_ocr_text(annotations)
                
                if parsed_history or current_game > 0:
                    new_history = [{"BR": "🟡 現在G", "ゲーム数": current_game}]
                    if parsed_history:
                        new_history.extend(parsed_history)
                    st.session_state.history_data = pd.DataFrame(new_history)
                    st.info(f"履歴テーブルを更新しました。誤りがあれば修正してください。")
                    st.rerun()
                else:
                    st.warning("画像から履歴データ(列・行)や現在のゲーム数を正しく認識できませんでした。")

st.divider()

# ==========================================
# 自動計算ロジック
# ==========================================
history = st.session_state.history_data.to_dict("records")

current_game = 0
history_bonuses = []
for row in history:
    if row.get("BR") == "🟡 現在G":
        try:
            current_game += int(row.get("ゲーム数", 0))
        except:
            pass
    else:
        history_bonuses.append(row)

history_reversed = list(reversed(history_bonuses))

# 有利区間リセット地点判定 (手動指定優先、なければ自動32Gで探す)
origin_idx = 0
if st.session_state.get("force_origin_idx") is not None:
    origin_idx = st.session_state.force_origin_idx
else:
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

# 先に累計G数と天井までの残りを計算する
total_games = 0
for i, row in enumerate(history_reversed):
    is_cut = i < origin_idx
    if not is_cut:
        try:
            g = int(row.get("ゲーム数", 0))
        except (ValueError, TypeError):
            g = 0
        b_type = row.get("BR", "🔴 BIG")
        start_g = total_games + g
        if "BIG" in b_type:
            total_games = start_g + 69
        else:
            total_games = start_g + 29
            
total_games += current_game
remaining_games = max(0, 2000 - total_games)

# サマリーダッシュボード
st.markdown(f"""
<div style="display:flex; justify-content:space-between; background:#333; color:#fff; padding:10px 15px; border-radius:12px; margin-bottom:15px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
    <div style="font-size:0.95em;">残り有利(2000G)<br><span style="font-size:1.6em;color:#ffc107;font-weight:900;">{remaining_games}G</span></div>
    <div style="font-size:0.95em; text-align:right;">計算対象 累計<br><span style="font-size:1.6em;color:#4facfe;font-weight:900;">{total_games}G</span></div>
</div>
""", unsafe_allow_html=True)

# カスタムコンポーネントの呼び出し
import streamlit.components.v1 as components
_frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
okidoki_keypad = components.declare_component("okidoki_keypad", path=_frontend_dir)

py_data = {
    "history": [{"BR": r.get("BR", "🔴 BIG"), "ゲーム数": int(r.get("ゲーム数", 0))} for r in history_reversed],
    "current_game": current_game,
    "origin_idx": origin_idx
}

result = okidoki_keypad(data=py_data, key="history_ui_instance")

if result is not None:
    new_origin = result.get("origin_idx", origin_idx)
    updated_history = result.get("history_updated", [])
    updated_current = result.get("current_game", current_game)
    
    changed = False
    if new_origin != origin_idx:
        changed = True
        
    if updated_current != current_game:
        changed = True
        
    if len(updated_history) != len(history_reversed):
        changed = True
    else:
        for i, row in enumerate(history_reversed):
            row_g = int(row.get("ゲーム数", 0))
            upd_g = int(updated_history[i].get("ゲーム数", 0))
            if row.get("BR") != updated_history[i].get("BR") or row_g != upd_g:
                changed = True
                break
                
    if changed:
        st.session_state.force_origin_idx = new_origin
        # 最新を上に再構築
        rebuilt = [{"BR": "🟡 現在G", "ゲーム数": updated_current}]
        rebuilt.extend(reversed(updated_history))
        st.session_state.history_data = pd.DataFrame(rebuilt)
        st.rerun()
