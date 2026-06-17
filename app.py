import streamlit as st
import pandas as pd
import random
import json
import google.genai as genai
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ==========================================
# 0. 🔑 安全な保管庫（Secrets）からすべての鍵を読み込む
# ==========================================
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")

# Google Cloudの鍵（JSON文字列）をSecretsから取得
GCP_JSON_STR = st.secrets.get("GCP_JSON", "")

st.set_page_config(page_title="完全無料：AI時間割スマート管理", layout="wide")
st.title("🧙‍♂️ 完全無料：AI×スプレッドシート 時間割自動作成アプリ")

# ==========================================
# 1. 🔗 スプレッドシートID入力枠
# ==========================================
st.subheader("🔗 1. データベース（スプレッドシート）連携")
spreadsheet_id = st.text_input(
    "GoogleスプレッドシートのIDを入力してください：",
    value=st.session_state.get("ss_id", ""),
    placeholder="URLの /d/ と /edit の間の文字列"
)

# ==========================================
# 2. ⚙️ 条件設定コントロールパネル（サイドバー）
# ==========================================
st.sidebar.header("⚙️ 2. 時間割の方針設定")
policy = st.sidebar.selectbox(
    "基本の配置バランス：",
    ["⚖️ 基本設定：バランスよく分散（推奨）", "🚀 前半詰め", "🌅 後半詰め"]
)

st.sidebar.markdown("---")
st.sidebar.subheader("⏱️ 教科の連続・間隔ルール")
interval_slots = st.sidebar.number_input(
    "同じ教科を次に配置するまで、最低何コマ空ける？",
    min_value=0, max_value=10, value=2, step=1
)

st.sidebar.subheader("🏫 教室のバッティング回避")
avoid_gym = st.sidebar.checkbox("体育館（体育）の重複を絶対に回避する", value=True)

st.sidebar.markdown("---")
user_requirements = st.sidebar.text_area(
    "🔧 3. 例外・こだわり条件（自由記述）", 
    placeholder="例：佐藤先生は1番〜5番は授業を入れないで。"
)

# ==========================================
# 3. 🧠 無料AI（Gemini）を使った条件解析関数
# ==========================================
def parse_requirements_with_gemini(text):
    if not text.strip():
        return {"teacher_ng": []}
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        system_prompt = """
        ユーザーの要望から1番〜25番における『NG（配置禁止）』を解析し、以下のJSON形式でのみ出力してください。
        {"teacher_ng": [{"name": "先生名", "start": 1, "end": 25}]}
        """
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"{system_prompt}\n\nユーザーの要望:\n{text}"
        )
        clean_text = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean_text)
    except:
        return {"teacher_ng": []}

# ==========================================
# 4. 🚀 メイン処理：データ読み込み＆時間割計算
# ==========================================
if st.button("🚀 重複ゼロ・全自動時間割を生成する"):
    if not spreadsheet_id:
        st.error("スプレッドシートIDを入力してください。")
        st.stop()
    if not GEMINI_API_KEY or not GCP_JSON_STR:
        st.error("必要な鍵（GeminiまたはGoogle Cloud）がSecretsに設定されていません。")
        st.stop()
        
    with st.spinner("スプレッドシートからマスタデータを読み込み、AIと計算中..."):
        try:
            # 🔒 ファイルではなく、文字列から直接認証情報を生成（安全）
            gcp_info = json.loads(GCP_JSON_STR)
            scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
            creds = ServiceAccountCredentials.from_json_keyfile_dict(gcp_info, scope)
            g_client = gspread.authorize(creds)
            
            sheet = g_client.open_by_key(spreadsheet_id)
            master_sheet = sheet.worksheet("マスタ")
            master_data = master_sheet.get_all_records()
            
            classes = sorted(list(set([row['クラス'] for row in master_data if row['クラス']])))
            teachers = sorted(list(set([row['先生'] for row in master_data if row['先生']])))
            
            sample_lessons = []
            for row in master_data:
                for _ in range(int(row['必須コマ数'])):
                    sample_lessons.append({"s": row['教科'], "t": row['先生'], "c": row['クラス']})
            
            if not sample_lessons:
                st.warning("マスタデータに有効な授業が登録されていません。")
                st.stop()
                
        except Exception as e:
            st.error(f"スプレッドシートの読み込みに失敗しました。: {e}")
            st.stop()

        ai_constraints = parse_requirements_with_gemini(user_requirements)
        slots = [f"{i}番" for i in range(1, 26)]
        timetable_df = pd.DataFrame(index=classes, columns=slots).fillna("")
        unplaced_lessons = []
        
        for lesson in sample_lessons:
            placed = False
            target_slots = list(range(1, 26))
            
            if "バランス" in policy:
                random.shuffle(target_slots)
            elif "後半詰め" in policy:
                target_slots.reverse() 
                
            for slot_num in target_slots:
                slot_name = f"{slot_num}番"
                if timetable_df.at[lesson['c'], slot_name] != "": continue
                
                teacher_busy = False
                for c in classes:
                    cell = timetable_df.at[c, slot_name]
                    if cell and f"({lesson['t']})" in cell:
                        teacher_busy = True
                        break
                if teacher_busy: continue
                
                if avoid_gym and lesson['s'] == "体育":
                    gym_busy = False
                    for c in classes:
                        cell = timetable_df.at[c, slot_name]
                        if cell and "体育" in cell:
                            gym_busy = True
                            break
                    if gym_busy: continue
                
                if interval_slots > 0:
                    too_close = False
                    start_check = max(1, slot_num - interval_slots)
                    end_check = min(25, slot_num + interval_slots)
                    for check_num in range(start_check, end_check + 1):
                        check_slot = f"{check_num}番"
                        cell = timetable_df.at[lesson['c'], check_slot]
                        if cell and lesson['s'] in cell:
                            too_close = True
                            break
                    if too_close: continue
                
                ai_ng = False
                for ng in ai_constraints.get("teacher_ng", []):
                    if ng["name"] == lesson["t"] and ng["start"] <= slot_num <= ng["end"]:
                        ai_ng = True
                        break
                if ai_ng: continue
                
                timetable_df.at[lesson['c'], slot_name] = f"{lesson['s']}\n({lesson['t']})"
                placed = True
                break
                
            if not placed: 
                unplaced_lessons.append(lesson)
                
        st.session_state["timetable"] = timetable_df
        st.session_state["unplaced"] = unplaced_lessons
        st.session_state["teachers"] = teachers
        st.session_state["classes"] = classes

# ==========================================
# 5. 📊 画面への結果出力
# ==========================================
if "timetable" in st.session_state:
    st.subheader("📊 4. 生成された時間割の確認")
    tab1, tab2 = st.tabs(["🏫 クラス別表示", "🧑‍🏫 先生別表示"])
    slots_names = [f"{i}番" for i in range(1, 26)]
    
    with tab1:
        st.dataframe(st.session_state["timetable"], use_container_width=True)
        
    with tab2:
        df_t = pd.DataFrame(index=st.session_state["teachers"], columns=slots_names).fillna("（空き）")
        for slot in slots_names:
            for c in st.session_state["classes"]:
                cell = st.session_state["timetable"].at[c, slot]
                if cell:
                    subj, teach = cell.split("\n")
                    t_name = teach.replace("(", "").replace(")", "")
                    if t_name in df_t.index:
                        df_t.at[t_name, slot] = f"{c}:{subj}"
        st.dataframe(df_t, use_container_width=True)

    st.markdown("---")
    st.subheader("⚠️ 5. 保留エリア")
    unplaced_list = st.session_state["unplaced"]
    if unplaced_list:
        st.error(f"自動配置できなかった授業があります。")
        st.dataframe(pd.DataFrame(unplaced_list))
    else:
        st.success("✨ 重複なし・最高のバランスで配置されました！")

    # ==========================================
    # 6. 💾 結果をスプレッドシートに書き戻す
    # ==========================================
    st.markdown("---")
    st.subheader("💾 6. 結果の保存")
    if st.button("📥 この時間割データをスプレッドシートに書き込む"):
        with st.spinner("シート『生成結果』に書き込み中..."):
            try:
                gcp_info = json.loads(GCP_JSON_STR)
                scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
                creds = ServiceAccountCredentials.from_json_keyfile_dict(gcp_info, scope)
                g_client = gspread.authorize(creds)
                sheet = g_client.open_by_key(spreadsheet_id)
                
                try:
                    ws = sheet.worksheet("生成結果")
                    sheet.del_worksheet(ws)
                except:
                    pass
                
                ws = sheet.add_worksheet(title="生成結果", rows="100", cols="30")
                output_df = st.session_state["timetable"].reset_index()
                ws.update([output_df.columns.values.tolist()] + output_df.values.tolist())
                st.success("🟢 書き込みが完了しました！")
            except Exception as e:
                st.error(f"書き込みエラー: {e}")
