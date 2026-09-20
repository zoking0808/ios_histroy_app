"""Public catalog queries, independent of WordPress and authenticated downloads."""
import datetime, json, re, threading, time
from collections import OrderedDict, deque
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

REGIONS = {'cn': '中国大陆', 'hk': '中国香港', 'tw': '中国台湾', 'us': '美国', 'jp': '日本', 'sg': '新加坡',
           'kr': '韩国', 'mo': '中国澳门', 'my': '马来西亚', 'th': '泰国', 'vn': '越南', 'ph': '菲律宾',
           'id': '印度尼西亚', 'in': '印度', 'pk': '巴基斯坦', 'au': '澳大利亚', 'nz': '新西兰', 'gb': '英国',
           'de': '德国', 'fr': '法国', 'it': '意大利', 'es': '西班牙', 'pt': '葡萄牙', 'nl': '荷兰', 'be': '比利时',
           'ch': '瑞士', 'at': '奥地利', 'se': '瑞典', 'no': '挪威', 'dk': '丹麦', 'fi': '芬兰', 'ie': '爱尔兰',
           'pl': '波兰', 'cz': '捷克', 'gr': '希腊', 'ro': '罗马尼亚', 'hu': '匈牙利', 'tr': '土耳其', 'ua': '乌克兰',
           'ru': '俄罗斯', 'ca': '加拿大', 'mx': '墨西哥', 'br': '巴西', 'ar': '阿根廷', 'cl': '智利', 'co': '哥伦比亚',
           'pe': '秘鲁', 'ae': '阿联酋', 'sa': '沙特阿拉伯', 'il': '以色列', 'qa': '卡塔尔', 'kw': '科威特',
           'eg': '埃及', 'za': '南非', 'ng': '尼日利亚', 'ke': '肯尼亚', 'ma': '摩洛哥'}


class CatalogError(Exception):
    def __init__(self, status, message): self.status, self.message = status, message


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def text(value, limit=200):
    return re.sub(r'[\x00-\x1f\x7f]', '', str(value))[:limit] if isinstance(value,
                                                                            (str, int, float)) and not isinstance(value,
                                                                                                                  bool) else ''


def valid_id(value): return isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{4,14}', value) is not None


def normalize_versions(items, source):
    rows = [];
    seen = set()
    for item in items[:3000]:
        if not isinstance(item, dict): continue
        id = text(
            next((item[k] for k in ('external_identifier', 'versionId', 'version_id', 'id') if item.get(k) is not None),
                 ''), 30)
        version = text(next(
            (item[k] for k in ('bundle_version', 'version', 'bundleShortVersionString') if item.get(k) is not None),
            ''), 100)
        if not re.fullmatch(r'[1-9][0-9]{0,19}', id) or not re.search(r'\d', version) or len(version) > 64 or (id,
                                                                                                               version) in seen: continue
        seen.add((id, version));
        date = ''
        raw = text(next(
            (item[k] for k in ('created_at', 'createTime', 'updateTime', 'date', 'time') if item.get(k) is not None),
            ''), 64)
        try:
            date = datetime.date.fromisoformat(raw[:10]).isoformat()
        except ValueError:
            pass
        rows.append({'versionId': id, 'version': version, 'recordedAt': date, 'source': source})

    def key(row):
        return tuple(
            (1, int(p)) if p.isdigit() else (0, p.lower()) for p in re.findall(r'\d+|\D+', row['version'])), int(
            row['versionId'])

    return sorted(rows, key=key, reverse=True)


class Catalog:
    def __init__(self, fetch=None):
        self.fetch = fetch or self.fetch_json;self.cache = OrderedDict();self.lock = threading.Lock();self.requests = deque()

    def fetch_json(self, url):
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.hostname not in ('itunes.apple.com', 'api.timbrd.com', 'app.agzy.cn',
                                                             'apis.bilin.eu.org') or parts.username or parts.password:
            raise CatalogError(400, '查询地址无效。')
        try:
            with build_opener(NoRedirect()).open(
                    Request(url, headers={'Accept': 'application/json', 'User-Agent': 'iOS-History-App/1.0'}),
                    timeout=7) as response:
                if response.status != 200: raise ValueError()
                body = response.read(2 * 1024 * 1024 + 1)
                if len(body) > 2 * 1024 * 1024: raise ValueError()
                return json.loads(body)
        except Exception:
            raise CatalogError(503, '数据源暂时不可用，请稍后重试。') from None

    def cached(self, key, loader, ttl):
        now = time.time()
        with self.lock:
            record = self.cache.get(key)
            if record and record[0] > now: self.cache.move_to_end(key);return record[1]
            while self.requests and self.requests[0] < now - 60: self.requests.popleft()
            if len(self.requests) >= 45: raise CatalogError(429, '查询服务较繁忙，请稍后再试。')
            self.requests.append(now)
        data = loader();
        short = not data.get('apps', data.get('versions', []))
        with self.lock:
            self.cache[key] = (time.time() + (60 if short else ttl), data);
            self.cache.move_to_end(key)
            while len(self.cache) > 256: self.cache.popitem(last=False)
        return data

    def apps(self, query, country='cn'):
        if not isinstance(query, str) or not 1 <= len(
            query.strip()) <= 1024 or country not in REGIONS: raise CatalogError(400, '应用名称或商店地区无效。')
        query = query.strip();
        id = query if valid_id(query) else ''
        if re.search(r'https?://|www\.', query, re.I):
            url = urlsplit(query)
            found = re.search(r'/id([1-9][0-9]{4,14})(?:/|$)', url.path)
            if url.scheme != 'https' or url.hostname != 'apps.apple.com' or url.username or url.password or url.port not in (
                    None, 443) or not found:
                raise CatalogError(400, '请填写应用名称、数字App ID或Apple App Store链接。')
            id = found[1]
        if not id and len(query) > 120: raise CatalogError(400, '应用名称过长。')

        def load():
            params = {'country': country, **({'id': id} if id else {'term': query, 'entity': 'software', 'limit': 18})}
            raw = self.fetch('https://itunes.apple.com/' + ('lookup' if id else 'search') + '?' + urlencode(params))
            if not isinstance(raw, dict) or not isinstance(raw.get('results'), list): raise CatalogError(502,
                                                                                                         'App Store返回格式异常。')
            apps = []
            for item in raw['results'][:18]:
                if not isinstance(item, dict) or item.get('wrapperType') != 'software' or not valid_id(
                    str(item.get('trackId', ''))): continue
                appid = str(item['trackId']);
                icon = text(item.get('artworkUrl100', ''), 2000);
                host = urlsplit(icon).hostname or ''
                if not icon.startswith('https://') or not (
                        host == 'mzstatic.com' or host.endswith('.mzstatic.com')): icon = ''
                apps.append(
                    {'id': appid, 'name': text(item.get('trackName')), 'developer': text(item.get('artistName')),
                     'bundleId': text(item.get('bundleId')), 'version': text(item.get('version'), 64),
                     'minimumOs': text(item.get('minimumOsVersion'), 30),
                     'price': text(item.get('formattedPrice')), 'icon': icon,
                     'storeUrl': f'https://apps.apple.com/{country}/app/id{appid}'})
            return {'apps': apps, 'country': country, 'source': 'Apple App Store',
                    'checkedAt': datetime.datetime.now(datetime.timezone.utc).isoformat()}

        return self.cached(('apps', id or query, country), load, 3600)

    def versions(self, id):
        if not valid_id(id): raise CatalogError(400, 'App ID无效。')

        def load():
            states = [];
            rows = [];
            selected = ''
            for source, url in [('Timbrd', f'https://api.timbrd.com/apple/app-version/index.php?id={id}'),
                                ('Agzy', f'https://app.agzy.cn/searchVersion?appid={id}'),
                                ('Bilin', f'https://apis.bilin.eu.org/history/{id}')]:
                try:
                    data = self.fetch(url);
                    items = data.get('data') if isinstance(data, dict) else data
                    if not isinstance(items, list): raise ValueError()
                    rows = normalize_versions(items, source)
                    if items and not rows: raise ValueError()
                    states.append({'source': source, 'status': 'ok' if rows else 'empty'})
                    if rows: selected = source;break
                except Exception:
                    states.append({'source': source, 'status': 'unavailable'});rows = []
            return {'appId': id, 'versions': rows, 'source': selected, 'providers': states,
                    'checkedAt': datetime.datetime.now(datetime.timezone.utc).isoformat()}

        return self.cached(('versions', id), load, 21600)
