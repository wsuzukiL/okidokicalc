import streamlit as st
import pandas as pd
import requests
import base64
import json
import re
import os

# OCRの準備: Google Cloud Vision API を使うための枠組み
# secrets.toml, 又は環境変数から取得。無ければサイドバーから入力可能にする。
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
if not GOOGLE_API_KEY:
    try:
        GOOGLE_API_KEY = st.secrets.get("GOOGLE_API_KEY", "")
    except (FileNotFoundError, KeyError):
        pass

# 画面構成: タイトル
st.title("沖ドキ！GOLD 有利区間計算ツール")

# サイドバーにAPIキー入力欄（環境変数/secretsに無い場合用）
with st.sidebar:
    st.header("⚙️ 設定")
    api_key_input = st.text_input("Google API Key", value=GOOGLE_API_KEY, type="password", help="Vision API 用のAPIキー。環境変数またはサイドバーから設定してください。")
    if api_key_input:
        GOOGLE_API_KEY = api_key_input

# OCR実行関数
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
        # textAnnotations には単語ごとのBoundingBox(座標)が格納されている
        return result["responses"][0]["textAnnotations"]
    except KeyError:
        return None

def parse_ocr_text(annotations):
    """
    Vision APIの textAnnotations (座標つき単語データ) を使い、
    同じ水平線上（同じY座標帯）にある「種類」と「ゲーム数」を正確にペアリングする。
    列の隙間が広くてバラバラにテキストとして読まれる現象を解決する。
    """
    if not annotations or len(annotations) < 2:
        return []
        
    full_text = annotations[0].get("description", "")
    words = annotations[1:]
    
    items = []
    for w in words:
        text = w.get("description", "").strip()
        poly = w.get("boundingPoly", {}).get("vertices", [])
        if len(poly) == 4 and text:
            y_centers = [v.get("y", 0) for v in poly]
            min_y, max_y = min(y_centers), max(y_centers)
            center_y = (min_y + max_y) / 2
            
            x_centers = [v.get("x", 0) for v in poly]
            min_x, max_x = min(x_centers), max(x_centers)
            center_x = (min_x + max_x) / 2
            
            items.append({
                "text": text,
                "x": center_x,
                "y": center_y,
                "height": max_y - min_y
            })
            
    if not items:
        return []
        
    # Y座標が近いもの同士をグループ化（行をつくる）
    items.sort(key=lambda item: item["y"])
    rows = []
    current_row = [items[0]]
    # 同じ行とみなすY軸のズレ許容値（文字の高さの半分程度）
    threshold = max(5, items[0]["height"] * 0.7) 
    
    for item in items[1:]:
        if abs(item["y"] - current_row[0]["y"]) < threshold:
            current_row.append(item)
        else:
            rows.append(current_row)
            current_row = [item]
            threshold = max(5, item["height"] * 0.7)
    
    if current_row:
        rows.append(current_row)
        
    # 各行の要素をX（左から右）へ並べ替え
    for r in rows:
        r.sort(key=lambda item: item["x"])
        
    # 行ごとに BB/RB と ゲーム数を探索
    bb_rb_pattern = re.compile(r'\b(BB|RB|BIG|REG|8B|R8)\b', re.IGNORECASE)
    number_pattern = re.compile(r'^(\d{1,4})(?:G|G\s*)?$')
    
    history_by_rows = []
    
    for r in rows:
        row_types = []
        row_games = []
        for item in r:
            text = item["text"].upper()
            
            if bb_rb_pattern.match(text) or "BIG" in text or "REG" in text or "BB" in text or "RB" in text:
                if 'BIG' in text or 'BB' in text or '8B' in text:
                    row_types.append('BB')
                else:
                    row_types.append('RB')
                    
            elif number_pattern.match(text) or text.isdigit():
                val = int(re.sub(r'[^0-9]', '', text))
                # 沖ドキのゲーム数にあり得る数字のみ (1〜2000)
                # かつ 1〜10の連番はグラフ縦軸のノイズになりやすいため、行の中にボーナス種別が無い単独の数字の場合はスキップする設計
                if 0 < val <= 2000:
                    row_games.append(val)
                    
        # この行内に種類とゲーム数が両方存在すればペアリング（横に並んでいるケース、表の1行等）
        length = min(len(row_types), len(row_games))
        for i in range(length):
            history_by_rows.append({"ゲーム数": row_games[i], "種類": row_types[i]})
            
    # もし横の行として全く抽出できなかった場合（完全に縦一列ずつBBBBB 309 146...と独立して書かれた例外レイアウトの場合の予備ロジック）
    if not history_by_rows:
        return fallback_parse_sequential(full_text)
        
    return history_by_rows

def fallback_parse_sequential(text):
    """
    以前までの単純な順次出現パース（縦に離れすぎている場合のフォールバック）
    """
    bb_rb_pattern = re.compile(r'\b(BB|RB|BIG|REG)\b', re.IGNORECASE)
    types = []
    for line in text.split('\n'):
        if "確率" in line or "過去" in line or "データ" in line:
            continue
        for match in bb_rb_pattern.finditer(line):
            val = match.group(1).upper()
            if 'BIG' in val or 'BB' in val:
                types.append('BB')
            else:
                types.append('RB')

    number_pattern = re.compile(r'^(\d{1,4})(?:G|G\s*)?$')
    games = []
    for line in text.split('\n'):
        line = line.strip()
        parts = line.split()
        for p in parts:
            match = number_pattern.match(p)
            val = None
            if match:
                val = int(match.group(1))
            elif p.isdigit():
                val = int(p)
            if val is not None and 0 < val <= 2000:
                games.append(val)
                
    seq = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    seq_str = ",".join(map(str, seq))
    games_str = ",".join(map(str, games))
    if seq_str in games_str:
        games_str = games_str.replace(seq_str, "")
        games = [int(x) for x in games_str.split(",") if x.strip()]
        
    length = min(len(types), len(games))
    history = []
    for i in range(length):
        history.append({"ゲーム数": games[i], "種類": types[i]})
        
    return history

# デフォルトのダミーデータ（動作確認用）
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

# 画面構成: 画像アップロード
st.markdown("### 画像アップロード (OCR用)")
uploaded_file = st.file_uploader("データカウンタの履歴画像をアップロードしてください", type=["jpg", "jpeg", "png"])

if uploaded_file is not None:
    if st.button("🔍 画像から履歴を読み取る"):
        if not GOOGLE_API_KEY:
            st.error("APIキーが設定されていません。サイドバーから入力するか、Streamlit CloudのSecretsを設定してください。")
        else:
            with st.spinner("画像を解析中..."):
                image_bytes = uploaded_file.getvalue()
                # 座標データを含んだ textAnnotations 全体を受け取る
                annotations = analyze_image_with_vision_api(image_bytes, GOOGLE_API_KEY)
                
                if annotations is not None and len(annotations) > 0:
                    st.success("テキスト情報を抽出・補正しました！")
                    
                    # 完全に生テキストのまま出力(デバッグ用)
                    full_text = annotations[0].get("description", "")
                    try:
                        with open("ocr_debug.txt", "w", encoding="utf-8") as f:
                            f.write(full_text)
                    except Exception:
                        pass
                        
                    parsed_history = parse_ocr_text(annotations)
                    
                    if parsed_history:
                        st.session_state.history_data = pd.DataFrame(parsed_history)
                        st.info("履歴テーブルを自動更新しました。誤りがあれば修正してください。")
                        # 画面をリロードして新しいDataFrameをst.data_editorに反映させる
                        st.rerun()
                    else:
                        st.warning("画像から履歴のゲーム数や種類を検出できませんでした。")

st.divider()

# 画面構成: 手動入力・履歴データテーブル
st.markdown("### ボーナス履歴")
st.caption("※上から「最新の履歴」になるように入力してください。現在のハマりゲーム数は下の入力欄に入力します。")

# st.data_editor を用いてユーザーが手動で編集・追加・削除できるようにする
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

# 計算実行ボタン
if st.button("計算する", type="primary"):
    # テーブルデータを辞書のリストに変換
    history = edited_df.to_dict("records")
    
    if not history:
        st.warning("履歴データを一つ以上入力してください。")
    else:
        # 計算ロジック:
        # リストは先頭(index 0)が最新、末尾が最古。
        # 古い順（最古から最新へ）に並び替えて、有利区間の起点を探す。
        history_reversed = list(reversed(history))
        
        origin_idx = 0
        prev_was_chain = False
        
        for i, row in enumerate(history_reversed):
            try:
                g = int(row.get("ゲーム数", 0))
            except (ValueError, TypeError):
                g = 0
                
            if g <= 32:
                # 32G以下は連チャン中
                prev_was_chain = True
            else:
                # 32Gを超えた初当たり
                # もし「前回が連チャン」だったなら、今回が「連チャン終了後の最初の初当たり」となる
                if prev_was_chain:
                    origin_idx = i
                # 初当たりを引いたことで連チャン状態を解除
                prev_was_chain = False
                
        # 起点(origin_idx)から最新までの累計ゲーム数を算出する
        total_games = 0
        
        # 起点から最新までのボーナス履歴を加算
        for i in range(origin_idx, len(history_reversed)):
            row = history_reversed[i]
            try:
                g = int(row.get("ゲーム数", 0))
            except (ValueError, TypeError):
                g = 0
            b_type = row.get("種類", "BB")
            
            # 通常当たりのゲーム数を加算
            total_games += g
            
            # ボーナス消化にかかるゲーム数を加算（BB=69G、RB=29G）
            if b_type == "BB":
                total_games += 69
            elif b_type == "RB":
                total_games += 29
                
        # 最後に、現在回っているハマりゲーム数を加算する
        total_games += current_game
        
        # 2000Gまでの残りを計算
        remaining_games = max(0, 2000 - total_games)
        
        st.divider()
        st.markdown("## 🎯 計算結果")
        
        # 大きく表示 (st.metricを使用)
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
