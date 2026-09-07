import base64
import hashlib
import json
import pytest
from crossborder_selector.models import Candidate
from crossborder_selector.probes.itdog import ItdogBackend, generate_guardret, task_token, parse_page

CFG = {"enabled": True, "timeout_s": 30, "nodes": {"1310": "telecom", "1273": "unicom", "1250": "mobile"}}
HTML = "<script>var wss_url='wss://ws.itdog.cn/x';var task_id='abc123';</script>"


def test_task_token_matches_reference():
    ref = hashlib.md5(("abc123" + "token_20230313000136kwyktxb0tgspm00yo5").encode()).hexdigest()[8:-8]
    assert task_token("abc123") == ref


def test_guardret_reference_algorithm():
    guard = "abcdefgh1234" + "21"
    key = "abcdefgh" + "PTNo2n3Ev5"
    val = str(21 * 2 + 16)
    enc = "".join(chr(ord(ch) ^ ord(key[i % len(key)])) for i, ch in enumerate(val))
    assert generate_guardret(guard) == base64.b64encode(enc.encode()).decode()


def test_parse_page():
    assert parse_page(HTML) == ("wss://ws.itdog.cn/x", "abc123")
    with pytest.raises(ValueError):
        parse_page("<html/>")


class Resp:
    def __init__(self, text): self.text = text


class FakeSession:
    def __init__(self):
        self.cookies, self.posts = {}, []
    def post(self, url, headers=None, data=None):
        self.posts.append((url, headers, data))
        if "guard" not in self.cookies:
            self.cookies["guard"] = "abcdefgh123421"
            return Resp("blocked")
        return Resp(HTML)


class FakeWs:
    def __init__(self, msgs): self.msgs, self.sent = list(msgs), []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def send(self, s): self.sent.append(s)
    def recv(self):
        if not self.msgs:
            raise TimeoutError("closed")
        return self.msgs.pop(0)


def test_full_flow_maps_nodes_to_isps():
    msgs = [json.dumps({"type": "data", "ip": "1.1.1.1", "result": "45", "node_id": "1310"}),
            json.dumps({"type": "data", "ip": "1.1.1.1", "result": "超时", "node_id": "1273"}),
            json.dumps({"type": "data", "ip": "2.2.2.2", "result": "80", "node_id": "1250"}),
            json.dumps({"type": "data", "ip": "1.1.1.1", "result": "70", "node_id": "9999"}),
            json.dumps({"type": "finished"})]
    ws, sess = FakeWs(msgs), FakeSession()
    b = ItdogBackend(CFG, session=sess, ws_connect=lambda url: ws)
    out = b.probe([Candidate("a", "1.1.1.1"), Candidate("b", "2.2.2.2")])
    assert len(sess.posts) == 2 and sess.cookies["guardret"] == generate_guardret("abcdefgh123421")
    assert sess.posts[1][2]["host"] == "1.1.1.1\r\n2.2.2.2" and sess.posts[1][2]["node_id"] == "1310,1273,1250"
    assert json.loads(ws.sent[0]) == {"task_id": "abc123", "task_token": task_token("abc123")}
    p1 = out["1.1.1.1"].probes
    assert [(p.isp, p.received, p.median_rtt_ms) for p in p1] == [("telecom", 1, 45.0), ("unicom", 0, None)]
    assert out["2.2.2.2"].probes[0].isp == "mobile"


def test_protocol_change_degrades_to_error():
    class Broken(FakeSession):
        def post(self, url, headers=None, data=None):
            self.cookies["guard"] = "abcdefgh123421"
            return Resp("<html>changed</html>")
    b = ItdogBackend(CFG, session=Broken(), ws_connect=lambda url: FakeWs([]))
    out = b.probe([Candidate("a", "1.1.1.1")])
    assert not out["1.1.1.1"].ok and "itdog" in out["1.1.1.1"].error
