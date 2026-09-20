import unittest
from urllib.parse import urlsplit,parse_qs
from app.catalog import Catalog,CatalogError,normalize_versions

class CatalogTests(unittest.TestCase):
    def test_normalization_and_order(self):
        rows=normalize_versions([
            {'versionId':'101','version':'8.0.9','date':'2026-01-01'},
            {'versionId':'102','version':'8.0.10','date':'not-a-date'},
            {'external_identifier':'102','bundle_version':'8.0.10'},
            {'versionId':'../1','version':'9'},None], 'fixture')
        self.assertEqual([r['version'] for r in rows],['8.0.10','8.0.9']);self.assertEqual(rows[0]['recordedAt'],'')
    def test_app_link_lookup_and_cache(self):
        calls=[]
        def fetch(url):
            calls.append(url);return {'results':[{'wrapperType':'software','trackId':414478124,'trackName':'WeChat','artworkUrl100':'https://evil.test/icon.png','version':'8.0.0'}]}
        api=Catalog(fetch)
        result=api.apps('https://apps.apple.com/cn/app/wechat/id414478124','cn')
        self.assertEqual(result['apps'][0]['id'],'414478124');self.assertEqual(result['apps'][0]['icon'],'')
        self.assertEqual(parse_qs(urlsplit(calls[0]).query)['id'],['414478124'])
        api.apps('414478124','cn');self.assertEqual(len(calls),1)
    def test_region_switch_uses_selected_store_and_separate_cache(self):
        calls=[]
        def fetch(url):
            region=parse_qs(urlsplit(url).query)['country'][0]
            calls.append(region)
            return {'results':[{'wrapperType':'software','trackId':414478124,'trackName':region}]}
        api=Catalog(fetch)
        for region in ('kr','gb','my','tr','ar','gb'):
            result=api.apps('414478124',region)
            self.assertEqual(result['country'],region)
            self.assertEqual(result['apps'][0]['name'],region)
            self.assertIn('/'+region+'/app/',result['apps'][0]['storeUrl'])
        self.assertEqual(calls,['kr','gb','my','tr','ar'])

    def test_unavailable_is_not_empty(self):
        def fetch(url):
            if 'timbrd' in url:raise CatalogError(503,'unavailable')
            return {'data':[]}
        data=Catalog(fetch).versions('414478124')
        self.assertEqual([p['status'] for p in data['providers']],['unavailable','empty','empty']);self.assertEqual(data['versions'],[])
    def test_rejects_non_apple_links_and_unknown_regions(self):
        def forbidden(url):self.fail('invalid input reached network')
        api=Catalog(forbidden)
        for query in ['http://127.0.0.1/','https://apps.apple.com.evil.test/app/id414478124','https://user:pass@apps.apple.com/app/id414478124']:
            with self.assertRaises(CatalogError):api.apps(query)
        with self.assertRaises(CatalogError):api.apps('test','xx')
if __name__=='__main__':unittest.main()
