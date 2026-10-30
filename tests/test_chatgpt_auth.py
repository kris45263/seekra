"""本地 OAuth 状态、身份和刷新验证；只使用测试令牌。"""
import importlib
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa


class ChatGPTAuthTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('seekra.providers.chatgpt_auth'), '需要独立的订阅授权入口')
        self.module=importlib.import_module('seekra.providers.chatgpt_auth')

    def test_state_identity_permission_and_protected_storage(self):
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()));jwk['kid']='test'
        with tempfile.TemporaryDirectory() as directory:
            auth=self.module.ChatGPTAuth(Path(directory),'default')
            pending=auth.prepare('http://127.0.0.1:12345/auth/callback')
            query=parse_qs(urlparse(pending['url']).query)
            self.assertEqual(query['client_id'],['dynamic_agent_client'])
            self.assertEqual(query['agent_name_hint'],['Seekra'])
            self.assertEqual(query['code_challenge_method'],['S256'])
            callback={'state':pending['state'],'code':'test-code','client_id':'oaiapp_test'}
            def respond(request):
                if request.url.path.endswith('jwks.json'):
                    return httpx.Response(200,json={'keys':[jwk]})
                token=jwt.encode({'iss':'https://auth.openai.com','aud':'oaiapp_test','sub':'test-sub',
                    'iat':int(time.time()),'exp':int(time.time())+3600,'nonce':pending['nonce']},key,algorithm='RS256',headers={'kid':'test'})
                return httpx.Response(200,json={'access_token':'secret-access','refresh_token':'secret-refresh',
                    'id_token':token,'token_type':'Bearer','expires_in':3600,
                    'scope':'openid offline_access resource.invoke chatgpt.tokens.use.direct'})
            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                with self.assertRaises(ValueError):
                    auth.complete(pending,{**callback,'state':'wrong'},client)
                self.assertFalse(auth.path.exists())
                auth.complete(pending,callback,client)
                self.assertEqual(auth.access_token(client),'secret-access')
            saved=auth.read()
            self.assertEqual(saved['subject'],'test-sub')
            self.assertEqual(saved['client_id'],'oaiapp_test')
            again=auth.prepare('http://127.0.0.1:54321/auth/callback')
            params=parse_qs(urlparse(again['url']).query)
            self.assertEqual(params['client_id'],['oaiapp_test'])
            self.assertEqual(params['ext_agent_host_id'],query['ext_agent_host_id'])
            self.assertNotIn('agent_name_hint',params)
            if __import__('os').name=='nt':
                self.assertNotIn(b'secret-access',auth.path.read_bytes())

    def test_invalid_identity_and_missing_permission_do_not_replace_credentials(self):
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()));jwk['kid']='test'
        for mode in ('nonce','audience','issuer','scope','subject'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                auth=self.module.ChatGPTAuth(Path(directory),'default')
                auth.write({'client_id':'oaiapp_test','subject':'original-sub','access_token':'original-token'})
                before=auth.path.read_bytes()
                pending=auth.prepare('http://127.0.0.1:12345/auth/callback')
                claims={'iss':'https://auth.openai.com','aud':'oaiapp_test','sub':'original-sub',
                        'iat':int(time.time()),'exp':int(time.time())+3600,'nonce':pending['nonce']}
                if mode!='scope':claims[{'nonce':'nonce','audience':'aud','issuer':'iss','subject':'sub'}[mode]]='wrong'
                token=jwt.encode(claims,key,algorithm='RS256',headers={'kid':'test'})
                def respond(request):
                    return httpx.Response(200,json={'keys':[jwk]} if request.url.path.endswith('jwks.json') else
                        {'access_token':'new-token','refresh_token':'new-refresh','id_token':token,
                         'token_type':'Bearer','expires_in':3600,'scope':'' if mode=='scope' else 'chatgpt.tokens.use.direct'})
                with httpx.Client(transport=httpx.MockTransport(respond)) as client,self.assertRaises(ValueError):
                    auth.complete(pending,{'state':pending['state'],'code':'test','client_id':'oaiapp_test'},client)
                self.assertEqual(auth.path.read_bytes(),before)

    def test_refresh_rotates_tokens_together_and_uses_issued_client(self):
        with tempfile.TemporaryDirectory() as directory:
            auth=self.module.ChatGPTAuth(Path(directory),'default')
            auth.write({'client_id':'oaiapp_test','subject':'sub','access_token':'expired',
                        'refresh_token':'old-refresh','expires_at':0,'scopes':['chatgpt.tokens.use.direct']})
            def respond(request):
                params=parse_qs(request.content.decode())
                self.assertEqual(params['client_id'],['oaiapp_test'])
                self.assertEqual(params['refresh_token'],['old-refresh'])
                self.assertNotIn('scope',params)
                return httpx.Response(200,json={'access_token':'new-access','refresh_token':'new-refresh',
                    'token_type':'Bearer','expires_in':3600,'scope':'chatgpt.tokens.use.direct'})
            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                self.assertEqual(auth.access_token(client),'new-access')
            self.assertEqual(auth.read()['refresh_token'],'new-refresh')

    def test_browser_callback_rejects_wrong_state_then_accepts_matching_login(self):
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()));jwk['kid']='test'
        captured={}; callback_results=[]; workers=[]
        def open_browser(url):
            params=parse_qs(urlparse(url).query);captured.update(params)
            def callback():
                with httpx.Client(trust_env=False) as browser:
                    for state in ('wrong',params['state'][0]):
                        response=browser.get(params['redirect_uri'][0],params={
                            'state':state,'code':'code','client_id':'oaiapp_test'},timeout=3)
                        callback_results.append(response.status_code)
            worker=threading.Thread(target=callback);workers.append(worker);worker.start()
            return True
        def respond(request):
            if request.url.path.endswith('jwks.json'):return httpx.Response(200,json={'keys':[jwk]})
            token=jwt.encode({'iss':'https://auth.openai.com','aud':'oaiapp_test','sub':'sub',
                'iat':int(time.time()),'exp':int(time.time())+3600,'nonce':captured['nonce'][0]},key,
                algorithm='RS256',headers={'kid':'test'})
            return httpx.Response(200,json={'access_token':'access','refresh_token':'refresh','id_token':token,
                'scope':'chatgpt.tokens.use.direct','token_type':'Bearer','expires_in':3600})
        with tempfile.TemporaryDirectory() as directory, httpx.Client(transport=httpx.MockTransport(respond)) as client:
            auth=self.module.ChatGPTAuth(Path(directory),'test')
            try:
                auth.login(client,open_browser=open_browser,timeout=5)
            finally:
                for worker in workers:worker.join(4)
            self.assertEqual(callback_results,[400,200])
            self.assertEqual(auth.read()['subject'],'sub')

    def test_profile_lock_prevents_concurrent_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            auth=self.module.ChatGPTAuth(Path(directory),'test')
            with self.module._lock(auth.path.with_suffix('.lock')):
                with self.assertRaises(RuntimeError):
                    auth.access_token(None)
