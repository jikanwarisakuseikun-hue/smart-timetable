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

if "GCP_JSON" in st.secrets:
    gcp_json_str = st.secrets["GCP_JSON"]
else:
    st.error("❌ StreamlitのAdvanced Settings（Secrets）内に 'GCP_JSON' が見つかりません。設定を確認してください。")
    st.stop()

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
    ["⚖️ 基本設定：バランスよく分散（推奨）", "🚀 前半詰め（午前重視）", "🌅 後半詰め（午後重視）"]
)

st.sidebar.markdown("---")
st.sidebar.subheader("⏱️ 教科の連続・間隔ルール")
interval_slots = st.sidebar.number_input(
    "同じ教科を次に配置するまで、最低何コマ空ける？（※通常授業のみ対象）",
    min_value=0, max_value=5, value=2, step=1
)

st.sidebar.markdown("---")
user_requirements = st.sidebar.text_area(
    "🔧 3. 例外・こだわり条件（自由記述）", 
    placeholder="例：美術の山本先生は学年が違っても必ず2コマ連続にしてください。佐藤先生は1番〜5番は授業を入れないで。"
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
        
    with st.spinner("スプレッドシートからマスタデータを読み込み、複雑な条件をパズル計算中..."):
        try:
            gcp_info = json.loads(gcp_json_str)
            scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
            creds = ServiceAccountCredentials.from_json_keyfile_dict(gcp_info, scope)
            g_client = gspread.authorize(creds)
            
            sheet = g_client.open_by_key(spreadsheet_id)
            master_sheet = sheet.worksheet("マスタ")
            master_data = master_sheet.get_all_records()
            
            classes = sorted(list(set([row['クラス'] for row in master_data if row['クラス']])))
            teachers = sorted(list(set([row['先生'] for row in master_data if row['先生']])))
            
            # 全授業データを展開
            all_lessons = []
            for row in master_data:
                for _ in range(int(row['必須コマ数'])):
                    all_lessons.append({
                        "s": row['教科'], 
                        "t": row['先生'], 
                        "c": row['クラス'],
                        "group_id": str(row.get('グループID', '')).strip(), # E列：101, 102
                        "ren_koma": str(row.get('連コマ', '')).strip(),     # F列：連
                        "gym": str(row.get('体育館', '')).strip()           # G列：○
                    })
            
            if not all_lessons:
                st.warning("マスタデータに有効な授業が登録されていません。")
                st.stop()
                
        except Exception as e:
            st.error(f"スプレッドシートの読み込みに失敗しました。列名（グループID, 連コマ, 体育館）が正しいか確認してください。: {e}")
            st.stop()

        # AIによる自由記述条件の解析
        ai_constraints = parse_requirements_with_gemini(user_requirements)
        
        slots = [f"{i}番" for i in range(1, 26)]
        timetable_df = pd.DataFrame(index=classes, columns=slots).fillna("")
        unplaced_lessons = []
        
        # --- 👑 特殊パズル処理：グループID（101と102をペアにして裏表かつ連続配置） ---
        group_lessons = [l for l in all_lessons if l['group_id'] != ""]
        normal_lessons = [l for l in all_lessons if l['group_id'] == ""]
        
        # 101, 102 などの末尾を切り分けて「10」という共通キーでペアを自動抽出
        paired_groups = {}
        for l in group_lessons:
            gid = l['group_id']
            base = gid[:-1]   # 「10」
            suffix = gid[-1]  # 「1」または「2」
            
            if base not in paired_groups:
                paired_groups[base] = {"1": [], "2": []}
            paired_groups[base][suffix].append(l)

        # パズルの配置順序（ポリシー反映）
        target_slots = list(range(1, 26))
        if "バランス" in policy:
            random.shuffle(target_slots)
        elif "後半詰め" in policy:
            target_slots.reverse()

        # A. 【最優先】101（前コマ合同）と102（次コマ合同）をガチッと連続配置
        for base, suffixes in paired_groups.items():
            list_1 = suffixes.get("1", []) # 101チーム
            list_2 = suffixes.get("2", []) # 102チーム
            
            placed_pair = False
            for slot_num in target_slots:
                slot_num_next = slot_num + 1
                # 25番の次がない、または日を跨ぐ（5の倍数）の場合はスキップ
                if slot_num_next > 25 or slot_num % 5 == 0: continue
                
                slot_name_1 = f"{slot_num}番"
                slot_name_2 = f"{slot_num_next}番"
                
                conflict = False
                # 101チームと102チームが一括配置できるか同時検証
                for l1 in list_1:
                    if timetable_df.at[l1['c'], slot_name_1] != "": conflict = True; break
                    if any([f"({l1['t']})" in timetable_df.at[c, slot_name_1] for c in classes if c != l1['c']]): conflict = True; break
                    if l1['gym'] != "" and any([timetable_df.at[c, slot_name_1] != "" and "体育" in timetable_df.at[c, slot_name_1] for c in classes]): conflict = True; break
                    for ng in ai_constraints.get("teacher_ng", []):
                        if ng["name"] == l1['t'] and ng["start"] <= slot_num <= ng["end"]: conflict = True; break
                
                for l2 in list_2:
                    if timetable_df.at[l2['c'], slot_name_2] != "": conflict = True; break
                    if any([f"({l2['t']})" in timetable_df.at[c, slot_name_2] for c in classes if c != l2['c']]): conflict = True; break
                    if l2['gym'] != "" and any([timetable_df.at[c, slot_name_2] != "" and "体育" in timetable_df.at[c, slot_name_2] for c in classes]): conflict = True; break
                    for ng in ai_constraints.get("teacher_ng", []):
                        if ng["name"] == l2['t'] and ng["start"] <= slot_num_next <= ng["end"]: conflict = True; break
                
                if conflict: continue
                
                # エラーがなければ配置確定
                for l1 in list_1:
                    timetable_df.at[l1['c'], slot_name_1] = f"{l1['s']}\n({l1['t']})"
                for l2 in list_2:
                    timetable_df.at[l2['c'], slot_name_2] = f"{l2['s']}\n({l2['t']})"
                placed_pair = True
                break
                
            if not placed_pair:
                unplaced_lessons.extend(list_1)
                unplaced_lessons.extend(list_2)

        # B. 【通常配置】残りの通常授業（連コマを含む）を処理
        for lesson in normal_lessons:
            placed = False
            is_renkoma = (lesson['ren_koma'] == "連")
            
            for slot_num in target_slots:
                slot_name = f"{slot_num}番"
                
                if is_renkoma:
                    slot_num_next = slot_num + 1
                    if slot_num_next > 25 or slot_num % 5 == 0: continue
                    slot_name_next = f"{slot_num_next}番"
                    
                    if timetable_df.at[lesson['c'], slot_name] != "" or timetable_df.at[lesson['c'], slot_name_next] != "": continue
                    if any([f"({lesson['t']})" in timetable_df.at[c, slot_name] for c in classes]) or any([f"({lesson['t']})" in timetable_df.at[c, slot_name_next] for c in classes]): continue
                    if lesson['gym'] != "" and (any([timetable_df.at[c, slot_name] != "" and "体育" in timetable_df.at[c, slot_name] for c in classes]) or any([timetable_df.at[c, slot_name_next] != "" and "体育" in timetable_df.at[c, slot_name_next] for c in classes])): continue
                    
                    # 自由記述（AI）チェック
                    ai_ng = False
                    for ng in ai_constraints.get("teacher_ng", []):
                        if ng["name"] == lesson['t'] and (ng["start"] <= slot_num <= ng["end"] or ng["start"] <= slot_num_next <= ng["end"]): ai_ng = True; break
                    if ai_ng: continue
                        
                    timetable_df.at[lesson['c'], slot_name] = f"{lesson['s']}\n({lesson['t']})"
                    timetable_df.at[lesson['c'], slot_name_next] = f"{lesson['s']}\n({lesson['t']})"
                    placed = True
                    break
                else:
                    if timetable_df.at[lesson['c'], slot_name] != "": continue
                    if any([f"({lesson['t']})" in timetable_df.at[c, slot_name] for c in classes]): continue
                    if lesson['gym'] != "" and any([timetable_df.at[c, slot_name] != "" and "体育" in timetable_df.at[c, slot_name] for c in classes]): continue
                        
                    if interval_slots > 0:
                        too_close = False
                        start_check = max(1, slot_num - interval_slots)
                        end_check = min(25, slot_num + interval_slots)
                        for check_num in range(start_check, end_check + 1):
                            if timetable_df.at[lesson['c'], f"{check_num}番"] and lesson['s'] in timetable_df.at[lesson['c'], f"{check_num}番"]:
                                too_close = True
                                break
                        if too_close: continue
                    
                    # 自由記述（AI）チェック
                    ai_ng = False
                    for ng in ai_constraints.get("teacher_ng", []):
                        if ng["name"] == lesson['t'] and ng["start"] <= slot_num <= ng["end"]: ai_ng = True; break
                    if ai_ng: continue
                    
                    timetable_df.at[lesson['c'], slot_name] = f"{lesson['s']}\n({lesson['t']})"
                    placed = True
                    break
                    
            if not placed and not is_renkoma: 
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
        st.error(f"自動配置できなかった授業があります。自由記述欄を調整するか条件を緩めて再試行してください。")
        st.dataframe(pd.DataFrame(unplaced_list))
    else:
        st.success("✨ 【101・102の連続裏表】・【単発連コマ】・【体育館被り回避】・【自由記述のAI条件】すべてを完璧にクリアしました！")

    # ==========================================
    # 6. 💾 結果をスプレッドシートに書き戻す
    # ==========================================
    st.markdown("---")
    st.subheader("💾 6. 結果の保存")
    if st.button("📥 この時間割データをスプレッドシートに書き込む"):
        with st.spinner("シート『生成結果』に書き込み中..."):
            try:
                gcp_info = json.loads(gcp_json_str)
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
                st.success("🟢 書き込みが完了しました！『生成結果』タブを確認してください。")
            except Exception as e:
                st.error(f"書き込みエラー: {e}")
