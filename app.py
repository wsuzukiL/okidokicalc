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
        text = result["responses"][0]["fullTextAnnotation"]["text"]
        return text
    except KeyError:
        return ""

def parse_ocr_text(text):
    """
    縦並び・横並びのどちらにも対応できるよう、ブロック化されたテキストをパースする。
    例 (ocr_debug.txt の傾向):
    BB \n BB \n BB \n RB \n RB ... (種類のブロック)
    309 \n 146 \n 256 \n 32 ... (ゲーム数のブロック)
    
    Vision APIは要素をブロックごとにまとめる傾向があるため、
    単純な出現順ではなく、種類リストとゲーム数リストを別々に抽出して若い順(上から)にペアリングする。
    """
    # 種類の抽出 (BB, BIG, RB, REG)
    bb_rb_pattern = re.compile(r'\b(BB|RB|BIG|REG)\b', re.IGNORECASE)
    types = []
    
    # BB確率や「1/356」などのノイズを避けるため、ある程度単独で存在するものを拾いたいが
    # まずは全件取得する
    for line in text.split('\n'):
        # 行の前後空白除去
        line = line.strip()
        # "BB確率" などの文字が含まれていたらスキップ
        if "確率" in line or "過去" in line or "データ" in line:
            continue
            
        # 種類の判定
        for match in bb_rb_pattern.finditer(line):
            val = match.group(1).upper()
            if 'BIG' in val or 'BB' in val:
                types.append('BB')
            else:
                types.append('RB')

    # ゲーム数の抽出 (1G〜2000G程度の数字)
    # 行全体が数字だけ、もしくは "123G" のようになっているものを優先して拾う
    number_pattern = re.compile(r'^(\d{1,4})(?:G|G\s*)?$')
    games = []
    
    # 全行をスキャンして、単独の数字行を抽出
    for line in text.split('\n'):
        line = line.strip()
        # 日付やスランプグラフのインデックス(1,2,3,4,5,6,7.. 10)と思われる数字を除外するため
        # また、全体スタート数(5348)や最大持ち玉(647)が混ざるのを防ぐため、
        # 典型的には「履歴の数字は連続して出現する」性質を利用するが、ここではシンプルに
        # 1〜2000以内の数字を拾う。ただし、1〜10の連番（インデックス）はノイズになりやすい。
        
        # 複数数字が1行にある場合 (例: "309 146 256") にも対応
        parts = line.split()
        for p in parts:
            match = number_pattern.match(p)
            val = None
            if match:
                val = int(match.group(1))
            elif p.isdigit(): # 正規表現にマッチしなくても単なる数字なら拾う
                val = int(p)
                
            if val is not None:
                # 1〜10はグラフの縦軸(10,9,8...1)のノイズの可能性が高い。
                # ただし実際の0G〜10G当たりの可能性もある。
                # デバッグテキストでは 10,9,8,7,6,5,4,3,2,1 の逆順が最初に来ている。
                # ここでは「10以下の連番っぽいもの」を取り除く厳密なロジックは難しいため、
                # BB/RBの個数と突き合わせることで後から削るアプローチをとる。
                if 0 < val <= 2000:
                    games.append(val)
                    
    # ノイズ除去処理
    # 1. グラフ縦軸マーカー (10, 9, 8, 7, 6, 5, 4, 3, 2, 1) が含まれている場合、それを削除
    # list 内に連続して降順の 10~1 があるか探す
    seq = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    seq_str = ",".join(map(str, seq))
    games_str = ",".join(map(str, games))
    if seq_str in games_str:
        # 見つけたら削除
        games_str = games_str.replace(seq_str, "")
        # カンマが連続する可能性があるので整理して再リスト化
        games = [int(x) for x in games_str.split(",") if x.strip()]
        
    # ペアリング
    # APIの読み取り順序上、履歴の上から下まで [BB, BB, RB, BB...] と読み、
    # そのあとゲーム数を [309, 146, 256, 32...] と読むことが多い。
    # したがって、純粋に前から順番にくっつけるのが最も正解に近い。
    
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
            st.error("APIキーが設定されていません。サイドバーから入力するか、環境変数を設定してください。")
        else:
            with st.spinner("画像を解析中..."):
                image_bytes = uploaded_file.getvalue()
                extracted_text = analyze_image_with_vision_api(image_bytes, GOOGLE_API_KEY)
                
                if extracted_text is not None:
                    # デバッグ用にファイルへ出力しておく（私が直接読み取って確認するため）
                    try:
                        with open("ocr_debug.txt", "w", encoding="utf-8") as f:
                            f.write(extracted_text)
                    except Exception:
                        pass
                        
                    # 改行文字の表示補正
                    st.success("テキスト情報を抽出しました！")
                    with st.expander("抽出された生テキスト (デバッグ用)", expanded=True):
                        st.text(extracted_text)
                    
                    # より詳細なデバッグ情報（APIレスポンスの形そのままで出力）
                    with st.expander("詳細な生テキストの構造 (開発・確認用)"):
                        st.text(repr(extracted_text))
                    
                    parsed_history = parse_ocr_text(extracted_text)
                    
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
