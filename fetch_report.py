import os
import sys
import re
import json
import time
import requests
from datetime import datetime, timedelta

# ============================================================
# НАСТРОЙКИ ПРОЕКТА — единственное, что нужно менять руками
# ============================================================
PLAN_DAILY_BUDGET = 100          # $3000/мес / 30 дней, ориентир на запуск
PLAN_MONTHLY_LEADS = 55          # по историческому CPL ~$54 и бюджету $3000
DAYS_IN_MONTH = 30
PLAN_MONTHLY_BUDGET = PLAN_DAILY_BUDGET * DAYS_IN_MONTH
TARGET_CPL = round(PLAN_MONTHLY_BUDGET / PLAN_MONTHLY_LEADS, 2)

FETCH_SINCE = '2025-07-01'       # тянем историю с июля 2025, как просили
CHUNK_DAYS = 7  # уменьшено с 30 — на 13+ месяцах daily-level истории широкий чанк даёт стабильный 5xx от Meta
API_VERSION = 'v25.0'
LEAD_ACTION_TYPES = {'lead'}      # см. checklist — проверить по факту через диагностику action_type

ACCESS_TOKEN = os.getenv('FACEBOOK_ACCESS_TOKEN')
ACCOUNT_ID = os.getenv('FACEBOOK_ACT_ID')

if not ACCESS_TOKEN or not ACCOUNT_ID:
    print("Ошибка: не заданы FACEBOOK_ACCESS_TOKEN или FACEBOOK_ACT_ID")
    sys.exit(1)

if not ACCOUNT_ID.startswith('act_'):
    ACCOUNT_ID = f'act_{ACCOUNT_ID}'

end_date = datetime.now().strftime('%Y-%m-%d')
start_date = FETCH_SINCE

# Читаемые названия целей кампаний Meta -> русские ярлыки для дашборда
OBJECTIVE_LABELS = {
    'OUTCOME_LEADS': 'Лиды',
    'LEAD_GENERATION': 'Лиды',
    'OUTCOME_TRAFFIC': 'Трафик',
    'LINK_CLICKS': 'Трафик',
    'OUTCOME_ENGAGEMENT': 'Вовлечённость',
    'POST_ENGAGEMENT': 'Вовлечённость',
    'OUTCOME_AWARENESS': 'Охват',
    'BRAND_AWARENESS': 'Охват',
    'REACH': 'Охват',
    'OUTCOME_SALES': 'Продажи',
    'CONVERSIONS': 'Продажи',
    'OUTCOME_APP_PROMOTION': 'Приложение',
    'PAGE_LIKES': 'Подписчики',
    'LIKES': 'Подписчики',
}


def date_chunks(since_str, until_str, chunk_days):
    since = datetime.strptime(since_str, '%Y-%m-%d')
    until = datetime.strptime(until_str, '%Y-%m-%d')
    cur = since
    while cur <= until:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), until)
        yield cur.strftime('%Y-%m-%d'), chunk_end.strftime('%Y-%m-%d')
        cur = chunk_end + timedelta(days=1)


class MetaAPIError(Exception):
    pass


def api_get(path, params):
    url = f"https://graph.facebook.com/{API_VERSION}/{path}"
    params = {**params, 'access_token': ACCESS_TOKEN}
    all_data = []

    while url:
        resp = None
        for attempt in range(3):
            try:
                resp = requests.get(url, params=params, timeout=120)
                if resp.status_code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                if resp.status_code >= 400:
                    print(f"Meta API вернул {resp.status_code} на {path}: {resp.text}")
                    if attempt == 2:
                        raise MetaAPIError(f"{resp.status_code} на {path}")
                    time.sleep(30 * (attempt + 1))
                    continue
                resp.raise_for_status()
                break
            except requests.exceptions.RequestException as e:
                if attempt == 2:
                    raise MetaAPIError(f"Ошибка запроса к {path}: {e}")
                time.sleep(2 ** attempt)
        else:
            raise MetaAPIError(f"Meta API стабильно возвращает 5xx на {path}")

        payload = resp.json()
        if 'error' in payload:
            raise MetaAPIError(f"Meta API вернул ошибку на {path}: {payload['error']}")

        all_data.extend(payload.get('data', []))
        url = payload.get('paging', {}).get('next')
        params = {}

    return all_data


def api_get_chunked(path, base_params, since, until):
    all_data = []
    for chunk_since, chunk_until in date_chunks(since, until, CHUNK_DAYS):
        params = {**base_params, 'time_range': json.dumps({'since': chunk_since, 'until': chunk_until})}
        all_data.extend(api_get(path, params))
    return all_data


def count_leads(actions):
    total = 0
    for action in actions or []:
        if action.get('action_type') in LEAD_ACTION_TYPES:
            total += int(action.get('value', 0))
    return total


def parse_language(name):
    # Реальный формат имён кампаний Elysium — тег в квадратных скобках в начале строки:
    # "[RU] [Трафик] Реклама в Instagram..." / "[EN] [Лиды] Leads | ..."
    name = name or ''
    bracket_match = re.search(r'\[([A-Za-zА-Яа-я]+)\]', name)
    if bracket_match:
        tag = bracket_match.group(1).upper()
        if tag in ('EN', 'ENG', 'ENGLISH'):
            return 'EN'
        if tag in ('RU', 'RUS', 'RUSSIAN'):
            return 'RU'
        if tag in ('KA', 'GEO', 'GEORGIAN', 'KAT'):
            return 'KA'
    # Фолбэк — старый формат через "_", на случай будущих кампаний с другим неймингом
    for p in re.split(r'[_\s]+', name):
        upper = p.upper()
        low = p.lower()
        if upper in ('EN', 'ENG', 'ENGLISH') or low in ('англ', 'английский'):
            return 'EN'
        if upper in ('RU', 'RUS', 'RUSSIAN') or low in ('ру', 'рус', 'русский'):
            return 'RU'
        if upper in ('KA', 'GEO', 'GEORGIAN', 'KAT') or low in ('гео', 'груз', 'грузинский'):
            return 'KA'
    return 'RU'


def parse_objective_tag(name):
    # Второй тег в скобках часто дублирует цель кампании прямо в названии —
    # используем как fallback/сверку, если поле objective из API вернёт что-то неожиданное
    name = name or ''
    tags = re.findall(r'\[([^\]]+)\]', name)
    for t in tags[1:2]:
        return t
    return None


def day_metrics(raw):
    spend = float(raw.get('spend', 0))
    leads = count_leads(raw.get('actions'))
    clicks = int(raw.get('clicks', 0))
    link_clicks = int(raw.get('inline_link_clicks', 0))
    impressions = int(raw.get('impressions', 0))
    return spend, leads, clicks, link_clicks, impressions


def dedup_by_date(raw_rows):
    by_date = {}
    for r in raw_rows:
        by_date[r['date_start']] = r
    result = []
    for date, r in sorted(by_date.items()):
        spend, leads, clicks, link_clicks, impressions = day_metrics(r)
        result.append({"date": date, "spend": round(spend, 2), "leads": leads, "clicks": clicks, "link_clicks": link_clicks, "impressions": impressions})
    return result


def dedup_by_entity_date(raw_rows, id_field, name_field, parent_fields=None):
    by_id = {}
    for r in raw_rows:
        entity_id = r.get(id_field)
        entry = by_id.setdefault(entity_id, {
            "id": entity_id,
            "name": r.get(name_field, ''),
            "parents": {pf: r.get(pf) for pf in (parent_fields or [])},
            "daily_by_date": {},
        })
        entry["daily_by_date"][r['date_start']] = r

    entities = []
    for entity_id, entry in by_id.items():
        daily = []
        for date, r in sorted(entry["daily_by_date"].items()):
            spend, leads, clicks, link_clicks, impressions = day_metrics(r)
            daily.append({"date": date, "spend": round(spend, 2), "leads": leads, "clicks": clicks, "link_clicks": link_clicks, "impressions": impressions})
        entity = {"id": entity_id, "name": entry["name"], "daily": daily}
        entity.update(entry["parents"])
        entities.append(entity)
    return entities


# ============================================================
# 1. Аккаунт — по дням
# ============================================================
account_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'fields': 'spend,clicks,inline_link_clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date)
account_daily = dedup_by_date(account_raw)

# ============================================================
# 2. Кампании — по дням + язык + статус + РЕАЛЬНАЯ цель (objective)
# ============================================================
campaigns_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'level': 'campaign',
    'fields': 'campaign_id,campaign_name,spend,clicks,inline_link_clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date)
campaigns = dedup_by_entity_date(campaigns_raw, 'campaign_id', 'campaign_name')
for c in campaigns:
    c["language"] = parse_language(c["name"])

campaign_meta_raw = api_get(f"{ACCOUNT_ID}/campaigns", {
    'fields': 'id,effective_status,objective',
    'limit': 200,
})
meta_by_campaign_id = {c['id']: c for c in campaign_meta_raw}
for c in campaigns:
    meta = meta_by_campaign_id.get(c["id"], {})
    c["status"] = meta.get('effective_status', 'UNKNOWN')
    raw_objective = meta.get('objective', '')
    c["objective"] = OBJECTIVE_LABELS.get(raw_objective, raw_objective or 'Другое')

print("Кампании — язык / цель:")
for c in campaigns:
    print(f"  [{c['language']}] [{c['objective']}] {c['name']}")

# ============================================================
# 3. Аудитории (adsets) и креативы (ads)
# ============================================================
adsets_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'level': 'adset',
    'fields': 'adset_id,adset_name,campaign_id,spend,clicks,inline_link_clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date)
adsets = dedup_by_entity_date(adsets_raw, 'adset_id', 'adset_name', parent_fields=['campaign_id'])

ads_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'level': 'ad',
    'fields': 'ad_id,ad_name,adset_id,campaign_id,spend,clicks,inline_link_clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date)
creatives = dedup_by_entity_date(ads_raw, 'ad_id', 'ad_name', parent_fields=['adset_id', 'campaign_id'])

ads_meta_raw = api_get(f"{ACCOUNT_ID}/ads", {
    'fields': 'id,creative.thumbnail_width(720).thumbnail_height(720){thumbnail_url}',
    'limit': 25,
})
thumb_by_ad_id = {a['id']: a.get('creative', {}).get('thumbnail_url') for a in ads_meta_raw}
for c in creatives:
    c["thumbnail_url"] = thumb_by_ad_id.get(c["id"])

# ============================================================
# 4. Демография, устройства, гео — необязательные разбивки:
#    если Meta стабильно отдаёт 5xx на какой-то из них (тяжёлый запрос
#    на длинной истории) — не роняем весь скрипт, просто оставляем пустым
# ============================================================
age_groups = []
try:
    age_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
        'time_increment': 1,
        'breakdowns': 'age,gender',
        'fields': 'spend,clicks,inline_link_clicks,impressions,actions',
        'limit': 500,
    }, start_date, end_date)

    demo_by_bucket = {}
    for r in age_raw:
        bucket = (r.get('age', 'unknown'), r.get('gender', 'unknown'))
        demo_by_bucket.setdefault(bucket, {})[r['date_start']] = r

    for (age, gender), by_date in demo_by_bucket.items():
        daily = []
        for date, r in sorted(by_date.items()):
            spend, leads, clicks, link_clicks, impressions = day_metrics(r)
            daily.append({"date": date, "spend": round(spend, 2), "leads": leads, "clicks": clicks, "link_clicks": link_clicks, "impressions": impressions})
        age_groups.append({"age": age, "gender": gender, "daily": daily})
except MetaAPIError as e:
    print(f"Пропускаю демографию — {e}")

devices = []
try:
    device_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
        'time_increment': 1,
        'breakdowns': 'impression_device',
        'fields': 'spend,clicks,inline_link_clicks,impressions,actions',
        'limit': 500,
    }, start_date, end_date)

    device_by_bucket = {}
    for r in device_raw:
        device_by_bucket.setdefault(r.get('impression_device', 'unknown'), {})[r['date_start']] = r

    for device, by_date in device_by_bucket.items():
        daily = []
        for date, r in sorted(by_date.items()):
            spend, leads, clicks, link_clicks, impressions = day_metrics(r)
            daily.append({"date": date, "spend": round(spend, 2), "leads": leads, "clicks": clicks, "link_clicks": link_clicks, "impressions": impressions})
        devices.append({"device": device, "daily": daily})
except MetaAPIError as e:
    print(f"Пропускаю устройства — {e}")

geo = []
try:
    geo_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
        'time_increment': 1,
        'level': 'campaign',
        'breakdowns': 'country',
        'fields': 'campaign_id,spend,clicks,inline_link_clicks,impressions,actions',
        'limit': 500,
    }, start_date, end_date)

    geo_by_bucket = {}
    for r in geo_raw:
        bucket = (r.get('campaign_id'), r.get('country', 'unknown'))
        geo_by_bucket.setdefault(bucket, {})[r['date_start']] = r

    for (campaign_id, country), by_date in geo_by_bucket.items():
        daily = []
        for date, r in sorted(by_date.items()):
            spend, leads, clicks, link_clicks, impressions = day_metrics(r)
            daily.append({"date": date, "spend": round(spend, 2), "leads": leads, "clicks": clicks, "link_clicks": link_clicks, "impressions": impressions})
        geo.append({"campaign_id": campaign_id, "country": country, "daily": daily})
except MetaAPIError as e:
    print(f"Пропускаю гео — {e}")


def fetch_reach(since, until, campaign_ids=None):
    params = {'time_range': json.dumps({'since': since, 'until': until}), 'fields': 'reach', 'limit': 1}
    if campaign_ids:
        params['filtering'] = json.dumps([{'field': 'campaign.id', 'operator': 'IN', 'value': campaign_ids}])
    try:
        raw = api_get(f"{ACCOUNT_ID}/insights", params)
        return int(raw[0].get('reach', 0)) if raw else 0
    except MetaAPIError as e:
        print(f"Пропускаю охват за {since}-{until} — {e}")
        return 0


end_dt = datetime.strptime(end_date, '%Y-%m-%d')
month_start = end_date[:8] + '01'
objectives_list = sorted(set(c['objective'] for c in campaigns))

reach_by_preset = {'all': {
    '7d': fetch_reach((end_dt - timedelta(days=6)).strftime('%Y-%m-%d'), end_date),
    '14d': fetch_reach((end_dt - timedelta(days=13)).strftime('%Y-%m-%d'), end_date),
    '30d': fetch_reach((end_dt - timedelta(days=29)).strftime('%Y-%m-%d'), end_date),
    'month': fetch_reach(month_start, end_date),
    'all': fetch_reach(start_date, end_date),
}}
for obj in objectives_list:
    ids = [c['id'] for c in campaigns if c['objective'] == obj]
    reach_by_preset[obj] = {
        '7d': fetch_reach((end_dt - timedelta(days=6)).strftime('%Y-%m-%d'), end_date, ids),
        '14d': fetch_reach((end_dt - timedelta(days=13)).strftime('%Y-%m-%d'), end_date, ids),
        '30d': fetch_reach((end_dt - timedelta(days=29)).strftime('%Y-%m-%d'), end_date, ids),
        'month': fetch_reach(month_start, end_date, ids),
        'all': fetch_reach(start_date, end_date, ids),
    }

report_data = {
    "last_updated": datetime.now().strftime('%d.%m.%Y, %H:%M'),
    "fetched_range": {"since": start_date, "until": end_date},
    "plan": {
        "monthly_budget": PLAN_MONTHLY_BUDGET,
        "monthly_leads": PLAN_MONTHLY_LEADS,
        "target_cpl": TARGET_CPL,
        "per_objective_budget": round(PLAN_MONTHLY_BUDGET / max(len(objectives_list), 1), 2),
    },
    "objectives": objectives_list,
    "account_daily": account_daily,
    "campaigns": campaigns,
    "adsets": adsets,
    "creatives": creatives,
    "age_groups": age_groups,
    "devices": devices,
    "geo": geo,
    "reach_by_preset": reach_by_preset,
}

os.makedirs('data', exist_ok=True)
with open('data/report.json', 'w', encoding='utf-8') as f:
    json.dump(report_data, f, ensure_ascii=False, indent=2)

total_spend = sum(d['spend'] for d in account_daily)
total_leads = sum(d['leads'] for d in account_daily)
print(f"Готово: {len(account_daily)} дней, {len(campaigns)} кампаний, {len(adsets)} аудиторий, {len(creatives)} объявлений, цели: {objectives_list}.")
print(f"Итого за период {start_date} — {end_date}: расход ${total_spend:.2f}, лиды {total_leads}.")
