"""Stateful HTTP-level CRPT substitute. Never used by main:create_app."""
import json
from uuid import uuid4
import httpx
from km_split.clients import Gateway
from km_split.codes import parse, GS, RS

SOURCE = '0104640638345218215SOURCE0000000000000' + GS + '91EE12' + GS + '92' + 'S' * 43 + '='
NEW = '010464063834521821' + '5NEW'.ljust(20, '0') + GS + '91EE12' + GS + '92' + 'N' * 43 + '='


class LiveFixture:
    def __init__(self, reject_oms_auth_once=False):
        self.calls=[]
        self.quantity=100
        self.separated=None
        self.order_id=str(uuid4())
        self.report_id=str(uuid4())
        self.block_id=str(uuid4())
        self.inn='7700000000'
        self.reject_oms_auth_once=reject_oms_auth_once

    def handle(self, request):
        self.calls.append(request)
        path=request.url.path
        if path.endswith('/auth/key'):
            return httpx.Response(200,json={'uuid':str(uuid4()),'data':'SIGN-THIS-UTF8-CHALLENGE'})
        if '/auth/simpleSignIn' in path:
            body=json.loads(request.content)
            assert body['inn']==self.inn and body['data']
            if path.endswith('/auth/simpleSignIn'):
                assert body['unitedToken'] is True
                return httpx.Response(200,json={'uuidToken':'fixture-true-token'})
            if self.reject_oms_auth_once:
                self.reject_oms_auth_once=False
                return httpx.Response(400,json={'code':'003','error_message':'Идентификатор соединения для указанного УОТ не найден'})
            return httpx.Response(200,json={'token':'fixture-oms-token'})
        if path.endswith('/cises/info'):
            assert request.headers['Authorization']=='Bearer fixture-true-token'
            assert json.loads(request.content)==[parse(SOURCE).cis]
            return httpx.Response(200,json=[{'cisInfo':{'requestedCis':parse(SOURCE).cis,
                'ownerInn':self.inn,'status':'INTRODUCED','quantityInPack':self.quantity}}])
        if path.endswith('/product/info'):
            assert json.loads(request.content)=={'gtins':[parse(SOURCE).gtin]}
            return httpx.Response(200,json={'results':[{'gtin':parse(SOURCE).gtin,'model':'PILOT-PN-100'}]})
        assert request.headers['clientToken']=='fixture-oms-token'
        assert request.url.params.get('omsId')
        if path=='/api/v3/order':
            assert request.headers['X-Signature']
            order=json.loads(request.content)
            assert order['products']==[{'gtin':parse(SOURCE).gtin,'quantity':1,'serialNumberType':'OPERATOR','templateId':36,'cisType':'UNIT'}]
            assert order['attributes']['releaseMethodType']=='REAPPLY'
            assert order['attributes']['paymentType']==1
            return httpx.Response(200,json={'orderId':self.order_id})
        if path=='/api/v3/order/status':
            assert request.url.params['orderId']==self.order_id
            return httpx.Response(200,json=[{'gtin':parse(SOURCE).gtin,'bufferStatus':'ACTIVE','availableCodes':1}])
        if path=='/api/v3/codes':
            assert request.url.params['quantity']=='1'
            return httpx.Response(200,json={'codes':[NEW],'blockId':self.block_id})
        if path=='/api/v3/utilisation':
            assert request.headers['X-Signature']
            body=json.loads(request.content)
            assert body['utilisationType']=='DIVISIONMARK'
            assert body['attributes']=={'participantId':self.inn}
            assert len(body['sntins'])==1
            new, source=body['sntins'][0].split(RS)
            assert source==SOURCE and parse(new).cis==parse(NEW).cis
            self.separated=parse(new).quantity
            return httpx.Response(200,json={'reportId':self.report_id})
        if path=='/api/v3/report/info':
            return httpx.Response(200,json={'reportStatus':'SUCCESS'})
        if path=='/api/v3/receipts/receipt':
            self.quantity=100-self.separated
            return httpx.Response(200,json={'results':[{'sourceDocId':self.report_id,'state':'SUCCESS','code':0}]})
        raise AssertionError(str(request.url))

    def gateway(self, settings, store):
        return Gateway(settings,store,httpx.MockTransport(self.handle))


def create_fixture_app():
    import os
    from main import create_app
    return create_app(gateway_factory=LiveFixture(os.getenv('SPLIT_FIXTURE_AUTH_REJECTION')=='1').gateway)
