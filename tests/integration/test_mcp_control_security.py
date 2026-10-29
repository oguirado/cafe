"""Parent-written regressions for actual advocate findings; no native success claim."""
import hashlib
import json
import os
import time
import unittest
from unittest import mock
import test_mcp_control as fixtures
from test_mcp_control import LoopbackBroker, make_wire, make_snapshot, approval_for, NOW, CAPSULE, ctl, mcp, initialize, request


class ParentSecurity(unittest.TestCase):
    setUp=fixtures.ControlTest.setUp
    plane=fixtures.ControlTest.plane
    reply_of=fixtures.ControlTest.reply_of
    audit_lines=fixtures.ControlTest.audit_lines

    def test_default_mcp_route_uses_the_control_monotonic_domain(self):
        issued=time.monotonic_ns()
        with LoopbackBroker() as broker:
            plane=ctl.ControlPlane(self.audit,snapshot_provider=make_snapshot,approval_provider=approval_for(make_wire(),issued),rendezvous_dir=self.root,ticket_fetcher=broker.fetch_ticket,connector=broker.connect)
            server=mcp.Server(self.root,control=plane);initialize(server)
            response=server.handle(request('tools/call',{'name':'start_recording','arguments':{'request_id':'req-1','capsule_id':CAPSULE}}))
            self.assertEqual(response['result']['structuredContent']['state'],'committed')
            self.assertEqual(len(broker.requests),1)

    def test_bare_wire_cannot_restamp_an_aged_approval(self):
        plane=self.plane(snapshot_provider=make_snapshot,approval_provider=lambda *a:make_wire())
        result=self.reply_of(plane.execute('start_recording',{'request_id':'req-1','capsule_id':CAPSULE},NOW))
        self.assertEqual((result['state'],result['error']),('rejected','invalid_control_intent'))

    def test_confirmed_token_in_identifier_is_not_reflected(self):
        token='canary_'+'x'*32;wire=make_wire(request_id=token,token=token)
        plane=self.plane(snapshot_provider=make_snapshot,approval_provider=approval_for(wire))
        reply=plane.execute('start_recording',{'request_id':token,'capsule_id':CAPSULE},NOW)
        self.assertNotIn(token,json.dumps(reply));self.assertNotIn(token,self.audit.read_text())

    def test_unknown_revision_never_skips_binding(self):
        plane=self.plane(snapshot_provider=lambda:make_snapshot(revision=None),approval_provider=approval_for(make_wire()))
        result=self.reply_of(plane.execute('start_recording',{'request_id':'req-1','capsule_id':CAPSULE},NOW))
        self.assertEqual((result['state'],result['error']),('rejected','stale_revision'))

    def test_request_cache_saturation_blocks_instead_of_forgetting(self):
        plane=self.plane(snapshot_provider=make_snapshot,approval_provider=approval_for(make_wire()))
        plane._seen['older-request']=True
        with mock.patch.object(ctl,'_SEEN_CAP',1):
            result=self.reply_of(plane.execute('start_recording',{'request_id':'req-1','capsule_id':CAPSULE},NOW))
        self.assertEqual((result['state'],result['error']),('blocked','confirmation_capacity'))
        self.assertIn('older-request',plane._seen)

    def test_token_cache_saturation_blocks_instead_of_evicting(self):
        plane=self.plane(snapshot_provider=make_snapshot,approval_provider=approval_for(make_wire()))
        old=hashlib.sha256(b'older-token').hexdigest();plane._consumed_tokens.add(old)
        with mock.patch.object(ctl,'_TOKEN_CAP',1):
            result=self.reply_of(plane.execute('start_recording',{'request_id':'req-1','capsule_id':CAPSULE},NOW))
        self.assertEqual((result['state'],result['error']),('blocked','confirmation_capacity'))
        self.assertIn(old,plane._consumed_tokens)

    def test_monotonic_issued_approval_is_not_compared_to_wall_clock(self):
        issued=time.monotonic_ns()
        plane=ctl.ControlPlane(self.audit,snapshot_provider=make_snapshot,approval_provider=approval_for(make_wire(),issued))
        result=self.reply_of(plane.execute('start_recording',{'request_id':'req-1','capsule_id':CAPSULE}))
        self.assertEqual((result['state'],result['error']),('blocked','mcp_control_unavailable'))

    def test_partial_audit_write_is_completed_before_selection(self):
        plane=self.plane(snapshot_provider=make_snapshot);original=os.write;calls=[]
        def partial(fd,data):calls.append(len(data));return original(fd,data[:7])
        with mock.patch.object(ctl.os,'write',side_effect=partial):
            result=self.reply_of(plane.execute('select_capsule',{'capsule_id':CAPSULE},NOW))
        self.assertEqual(result['state'],'selected');self.assertGreater(len(calls),1)
        self.assertEqual(len(self.audit_lines()),1)

    def test_zero_progress_audit_write_prevents_selection(self):
        plane=self.plane(snapshot_provider=make_snapshot)
        with mock.patch.object(ctl.os,'write',return_value=0):
            result=self.reply_of(plane.execute('select_capsule',{'capsule_id':CAPSULE},NOW))
        self.assertEqual((result['state'],result['error']),('blocked','audit_unavailable'))
        self.assertIsNone(plane.selected_capsule)

    def test_hardlinked_audit_file_is_not_modified(self):
        self.audit.write_bytes(b'unchanged');other=self.root/'other.jsonl';os.link(self.audit,other)
        plane=self.plane(snapshot_provider=make_snapshot)
        result=self.reply_of(plane.execute('select_capsule',{'capsule_id':CAPSULE},NOW))
        self.assertEqual(result['state'],'blocked');self.assertEqual(other.read_bytes(),b'unchanged')

    def test_failed_outcome_audit_never_claims_committed_or_retries(self):
        with LoopbackBroker() as broker:
            plane=self.plane(snapshot_provider=make_snapshot,approval_provider=approval_for(make_wire()),rendezvous_dir=self.root,ticket_fetcher=broker.fetch_ticket,connector=broker.connect)
            with mock.patch.object(plane,'_append_audit',side_effect=[True,False]):
                result=self.reply_of(plane.execute('start_recording',{'request_id':'req-1','capsule_id':CAPSULE},NOW))
            self.assertEqual((result['state'],result['error']),('interrupted','audit_unavailable'));self.assertIsNone(result['receipt'])
            self.assertEqual(len(broker.requests),1)
            again=self.reply_of(plane.execute('start_recording',{'request_id':'req-1','capsule_id':CAPSULE},NOW))
            self.assertEqual(again['state'],'blocked');self.assertEqual(len(broker.requests),1)
            self.assertIn('req-1',plane.uncertain_requests)


if __name__=='__main__':unittest.main()
