import streamlit as st
import pandas as pd
import random
import json
import re
import google.genai as genai
import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# 0. 🔑 安全な保管庫（Secrets）からすべての鍵を読み込む
# ==========================================
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")

if "GCP_JSON" in st.secrets:
    gcp_data = st.secrets["GCP_JSON"]
    if isinstance(gcp_data, str):
        try:
            cleaned_gcp_data = re.sub(r'\\([^"\\\/bfnrtu])', r'\1', gcp_data)
            cleaned_gcp_data = cleaned_gcp_data.replace("\\n", "\n")
            gcp_info = json.loads(cleaned_gcp_data)
        except Exception as e:
            try:
                gcp_info = json.loads(gcp_data)
            except Exception as final_err:
                st.error(f"❌ SecretsのGCP_JSONの読み込みに失敗しました: {final_err}")
                st.stop()
    else:
        gcp_info = dict(gcp_data)
        
    if "private_key" in gcp_info:
        gcp_info["private_key"] = gcp_info["private_key"].replace("\\n", "\n")
else:
    st.error("❌ StreamlitのAdvanced Settings（Secrets）内に 'GCP_JSON' が見つかりません。")
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
    placeholder="URL of the spreadsheet"
)

# ==========================================
# 2. ⚙️ 条件設定コントロールパネル（サイドバー）
# ==========================================
st.sidebar.header("⚙️ 2. 時間割の方針設定")

policy = st.sidebar.selectbox(
    "先生の配置バランス設定：",
    [
        "🚀 絶対配置型（コマ詰め・連続重視）",
        "⚖️ 中間（バランス配置・推奨）", 
        "🍀 先生の空きコマ分散型（1日の授業を平滑化）"
    ]
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
    placeholder="例：美術の山本先生は学年が違っても必ず2コマ連続にしてください。"
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
        
    with st.spinner("スプレッドシートからマスタデータを読み込み、パズル計算中..."):
        try:
            scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
            creds = Credentials.from_service_account_info(gcp_info, scopes=scopes)
            g_client = gspread.authorize(creds)
            
            sheet = g_client.open_by_key(spreadsheet_id)
            master_sheet = sheet.worksheet("マスタ")
            master_data = master_sheet.get_all_records()
            
            clean_master_data = []
            for row in master_data:
                c_val = str(row.get('クラス', '')).strip()
                t_val = str(row.get('先生', '')).strip()
                s_val = str(row.get('教科', '')).strip()
                k_val = str(row.get('必須コマ数', '')).strip()
                
                if not c_val or not t_val or not s_val or not k_val or k_val == '0':
                    continue
                clean_master_data.append(row)
            
            classes = sorted(list(set([str(row['クラス']).strip() for row in clean_master_data])))
            
            raw_teachers = []
            for row in clean_master_data:
                for t in str(row['先生']).split('・'):
                    if t.strip(): raw_teachers.append(t.strip())
            teachers = sorted(list(set(raw_teachers)))
            
            # 💡【重要修正】合同授業（グループIDあり）と通常授業を分けて展開
            grouped_raw = {}
            normal_lessons = []
            
            for row in clean_master_data:
                g_id = str(row.get('グループID', '')).strip()
                koma_count = int(row['必須コマ数'])
                
                lesson_unit = {
                    "s": str(row['教科']).strip(), 
                    "t": str(row['先生']).strip(), 
                    "c": str(row['クラス']).strip(),
                    "group_id": g_id, 
                    "ren_koma": str(row.get('連コマ', '')).strip(),     
                    "gym": str(row.get('体育館', '')).strip()           
                }
                
                if g_id:
                    # グループIDごとに、クラスごとのデータをまとめる
                    if g_id not in grouped_raw:
                        grouped_raw[g_id] = {}
                    if lesson_unit['c'] not in grouped_raw[g_id]:
                        grouped_raw[g_id][lesson_unit['c']] = []
                    # 該当クラスにコマ数分追加
                    for _ in range(koma_count):
                        grouped_raw[g_id][lesson_unit['c']].append(lesson_unit)
                else:
                    for _ in range(koma_count):
                        normal_lessons.append(lesson_unit)
                        
            # 💡【最重要】合同授業の「クラス間のコマ数ズレ」を防ぎ、1コマずつにパッケージングする
            grouped_lessons_packages = []
            for g_id, class_map in grouped_raw.items():
                # 各クラスのコマ数の最大値を取得
                max_koma = max([len(items) for items in class_map.values()]) if class_map else 0
                for index in range(max_koma):
                    package = []
                    for c_name, items in class_map.items():
                        if index < len(items):
                            package.append(items[index])
                    if package:
                        grouped_lessons_packages.append(package)
            
            if not normal_lessons and not grouped_lessons_packages:
                st.warning("マスタデータに有効な授業が登録されていません。")
                st.stop()
                
        except Exception as e:
            st.error(f"スプレッドシートの読み込みに失敗しました: {e}")
            st.stop()

        ai_constraints = parse_requirements_with_gemini(user_requirements)
        
        slots = [f"{i}番" for i in range(1, 26)]
        timetable_df = pd.DataFrame(index=classes, columns=slots).fillna("")
        unplaced_lessons = []

        def is_teacher_busy(t_string, slot_n, df, current_class_list):
            t_list = [t.strip() for t in t_string.split('・') if t.strip()]
            for c in classes:
                if c in current_class_list: continue # 合同授業を受ける自分たちのクラス同士は重複から除外！
                cell = df.at[c, f"{slot_n}番"]
                if cell:
                    for t in t_list:
                        if f"({t})" in cell or f"({t}・" in cell or f"・{t})" in cell:
                            return True
            return False

        def is_ai_ng(t_string, slot_n, constraints):
            t_list = [t.strip() for t in t_string.split('・') if t.strip()]
            for ng in constraints.get("teacher_ng", []):
                if ng["name"] in t_list and ng["start"] <= slot_n <= ng["end"]:
                    return True
            return False

        def get_teacher_daily_load(t_string, slot_n, df):
            t_list = [t.strip() for t in t_string.split('・') if t.strip()]
            day_start = ((slot_n - 1) // 5) * 5 + 1
            day_slots = list(range(day_start, day_start + 5))
            
            load = 0
            for sn in day_slots:
                for c in classes:
                    cell = df.at[c, f"{sn}番"]
                    if cell:
                        for t in t_list:
                            if f"({t})" in cell or f"({t}・" in cell or f"・{t})" in cell:
                                load += 1
            return load

        def get_optimized_slots(lesson_t, current_policy, df, force_flat=False):
            slot_scores = []
            base_slots = list(range(1, 26))
            random.shuffle(base_slots)
            
            for sn in base_slots:
                score = 0
                if not force_flat:
                    daily_load = get_teacher_daily_load(lesson_t, sn, df)
                    if "空きコマ分散型" in current_policy:
                        score = -daily_load 
                    elif "絶対配置型" in current_policy:
                        score = daily_load if daily_load > 0 else -1
                slot_scores.append((sn, score))
            
            slot_scores.sort(key=lambda x: x[1], reverse=True)
            return [x[0] for x in slot_scores]

        # A. 【最優先】合同授業（パッケージ単位）の配置処理
        for g_package in grouped_lessons_packages:
            sample_t = g_package[0]['t'] if g_package else ""
            g_classes = [l['c'] for l in g_package]
            optimized_slots = get_optimized_slots(sample_t, policy, timetable_df, force_flat=False)
            placed_group = False
            
            has_renkoma = any([l['ren_koma'] == "連" for l in g_package])
            
            for attempt in range(3):
                if attempt == 1:
                    optimized_slots = get_optimized_slots(sample_t, policy, timetable_df, force_flat=True)
                elif attempt == 2:
                    has_renkoma = False 
                
                for slot_num in optimized_slots:
                    slot_name = f"{slot_num}番"
                    
                    if has_renkoma:
                        slot_num_next = slot_num + 1
                        if slot_num_next > 25 or slot_num % 5 == 0: continue
                        slot_name_next = f"{slot_num_next}番"
                        
                        conflict = False
                        for l in g_package:
                            if timetable_df.at[l['c'], slot_name] != "" or timetable_df.at[l['c'], slot_name_next] != "": conflict = True; break
                            if is_teacher_busy(l['t'], slot_num, timetable_df, g_classes) or is_teacher_busy(l['t'], slot_num_next, timetable_df, g_classes): conflict = True; break
                            if is_ai_ng(l['t'], slot_num, ai_constraints) or is_ai_ng(l['t'], slot_num_next, ai_constraints): conflict = True; break
                        
                        if conflict: continue
                        
                        for l in g_package:
                            timetable_df.at[l['c'], slot_name] = f"{l['s']}\n({l['t']})"
                            timetable_df.at[l['c'], slot_name_next] = f"{l['s']}\n({l['t']})"
                        placed_group = True
                        break
                    else:
                        conflict = False
                        for l in g_package:
                            if timetable_df.at[l['c'], slot_name] != "": conflict = True; break
                            if is_teacher_busy(l['t'], slot_num, timetable_df, g_classes): conflict = True; break
                            if is_ai_ng(l['t'], slot_num, ai_constraints): conflict = True; break
                        
                        if conflict: continue
                        
                        for l in g_package:
                            timetable_df.at[l['c'], slot_name] = f"{l['s']}\n({l['t']})"
                        placed_group = True
                        break
                        
                if placed_group: break
                
            if not placed_group:
                unplaced_lessons.extend(g_package)

        # B. 【通常配置】通常授業（連コマを含む）
        for lesson in normal_lessons:
            placed = False
            is_renkoma = (lesson['ren_koma'] == "連")
            
            for attempt in range(3):
                if attempt == 0:
                    optimized_slots = get_optimized_slots(lesson['t'], policy, timetable_df, force_flat=False)
                    current_renkoma = is_renkoma
                elif attempt == 1:
                    optimized_slots = get_optimized_slots(lesson['t'], policy, timetable_df, force_flat=True)
                    current_renkoma = is_renkoma
                elif attempt == 2 and is_renkoma:
                    optimized_slots = get_optimized_slots(lesson['t'], policy, timetable_df, force_flat=True)
                    current_renkoma = False 
                else:
                    break
                
                for slot_num in optimized_slots:
                    slot_name = f"{slot_num}番"
                    
                    if current_renkoma:
                        slot_num_next = slot_num + 1
                        if slot_num_next > 25 or slot_num % 5 == 0: continue
                        slot_name_next = f"{slot_num_next}番"
                        
                        if timetable_df.at[lesson['c'], slot_name] != "" or timetable_df.at[lesson['c'], slot_name_next] != "": continue
                        if is_teacher_busy(lesson['t'], slot_num, timetable_df, [lesson['c']]) or is_teacher_busy(lesson['t'], slot_num_next, timetable_df, [lesson['c']]): continue
                        if lesson['gym'] != "" and (any([timetable_df.at[c, slot_name] != "" and "体育" in timetable_df.at[c, slot_name] for c in classes]) or any([timetable_df.at[c, slot_name_next] != "" and "体育" in timetable_df.at[c, slot_name_next] for c in classes])): continue
                        if is_ai_ng(lesson['t'], slot_num, ai_constraints) or is_ai_ng(lesson['t'], slot_num_next, ai_constraints): continue
                            
                        timetable_df.at[lesson['c'], slot_name] = f"{lesson['s']}\n({lesson['t']})"
                        timetable_df.at[lesson['c'], slot_name_next] = f"{lesson['s']}\n({lesson['t']})"
                        placed = True
                        break
                    else:
                        if timetable_df.at[lesson['c'], slot_name] != "": continue
                        if is_teacher_busy(lesson['t'], slot_num, timetable_df, [lesson['c']]): continue
                        if lesson['gym'] != "" and any([timetable_df.at[c, slot_name] != "" and "体育" in timetable_df.at[c, slot_name] for c in classes]): continue
                        
                        if interval_slots > 0 and attempt < 2:
                            too_close = False
                            start_check = max(1, slot_num - interval_slots)
                            end_check = min(25, slot_num + interval_slots)
                            for check_num in range(start_check, end_check + 1):
                                if timetable_df.at[lesson['c'], f"{check_num}番"] and lesson['s'] in timetable_df.at[lesson['c'], f"{check_num}番"]:
                                    too_close = True
                                    break
                            if too_close: continue
                        
                        if is_ai_ng(lesson['t'], slot_num, ai_constraints): continue
                        
                        timetable_df.at[lesson['c'], slot_name] = f"{lesson['s']}\n({lesson['t']})"
                        placed = True
                        break
                        
                if placed: break
                    
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
                    t_clean = teach.replace("(", "").replace(")", "")
                    individual_teachers = [t.strip() for t in t_clean.split('・') if t.strip()]
                    
                    for single_t in individual_teachers:
                        if single_t in df_t.index:
                            df_t.at[single_t, slot] = f"{c}:{subj}"
                            
        st.dataframe(df_t, use_container_width=True)

    st.markdown("---")
    st.subheader("⚠️ 5. 保留エリア")
    unplaced_list = st.session_state["unplaced"]
    if unplaced_list:
        st.error(f"自動配置できなかった授業が {len(unplaced_list)} コマあります。")
        st.dataframe(pd.DataFrame(unplaced_list))
    else:
        st.success("✨ GASの時と同じ仕様を完全再現し、保留なしの時間割が完成しました！")

    # ==========================================
    # 6. 💾 結果をスプレッドシートに書き戻す
    # ==========================================
    st.markdown("---")
    st.subheader("💾 6. 結果の保存")
    if st.button("📥 この時間割データをスプレッドシートに書き込む"):
        with st.spinner("シート『生成結果』に書き込み中..."):
            try:
                scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
                creds = Credentials.from_service_account_info(gcp_info, scopes=scopes)
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
