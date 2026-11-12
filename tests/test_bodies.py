"""The shared body and URL rules (zharp._bodies), the multipart reader and the taps (zharp._capture).

Expected values were produced by the JavaScript SDK's @zharp/internal/bodies from the same input,
so a payload reads the same whichever SDK sent it.
"""

import gzip
import json
import re
import unittest
import zlib

from zharp._bodies import (
    CaptureBodies,
    body_attributes,
    body_settings,
    describe_body,
    file_label,
    format_body,
    format_size,
    is_secret_key,
    is_textual,
    redact_query,
    redact_url,
    url_matches,
    wanted,
)
from zharp._capture import Multipart, Tap, TeeInput, bytes_attributes, side_attributes, unread_attributes

SETTINGS = body_settings({"mode": "all", "max_size": 200, "redact_keys": ["dateOfBirth"]})
IMAGE = "data:image/png;base64," + "iVBORw0KGgo" * 40


def fmt(text, content_type="application/json", cut=False, settings=SETTINGS):
    result = format_body(text, content_type, settings, cut)
    return None if result is None else {"value": result[0], "truncated": result[1]}


class SettingsTest(unittest.TestCase):
    def test_off_by_default(self):
        for value in (None, False, "", "off", "false"):
            self.assertIsNone(body_settings(value))

    def test_shapes(self):
        self.assertEqual(body_settings(True).mode, "errors")
        self.assertEqual(body_settings("all").mode, "all")
        self.assertEqual(body_settings("errors").max_size, 32 * 1024)
        self.assertEqual(body_settings(CaptureBodies(mode="all", max_size=10**9)).max_size, 128 * 1024)
        self.assertEqual(body_settings({"max_size": 4096, "nonsense": 1}).max_size, 4096)
        self.assertEqual(body_settings({"max_size": -1}).max_size, 32 * 1024)

    def test_environment(self):
        self.assertEqual(body_settings(None, "all").mode, "all")
        self.assertEqual(body_settings(None, "errors").mode, "errors")
        self.assertEqual(body_settings(None, "true").mode, "errors")
        self.assertIsNone(body_settings(None, "off"))
        self.assertEqual(body_settings("all", "off").mode, "all", "an explicit option wins")

    def test_wanted(self):
        errors = body_settings("errors")
        self.assertFalse(wanted(errors, 200))
        self.assertTrue(wanted(errors, 404))
        self.assertTrue(wanted(errors, None), "no response")
        self.assertTrue(wanted(body_settings("all"), 200))


class RulesTest(unittest.TestCase):
    def test_secret_keys(self):
        for key in ("password", "Password", "user_password", "apiKey", "X-Api-Key", "accessToken", "cvv", "pwd", "card", "Authorization", "session_id"):
            self.assertTrue(is_secret_key(key), key)
        for key in ("email", "name", "author", "pwdx", "cards", "key", "session", "sessions"):
            self.assertFalse(is_secret_key(key), key)
        self.assertTrue(is_secret_key("date_of_birth", ["dateOfBirth"]))
        self.assertTrue(is_secret_key("internalRef", [re.compile("Ref$")]))

    def test_json_keeps_names_and_filters_secret_values(self):
        out = json.loads(fmt('{"email":"a@b.test","password":"hunter2","dateOfBirth":"1990","list":[{"apiKey":"k","ok":true,"x":null}],"empty":""}')["value"])
        self.assertEqual(out, {"email": "a@b.test", "password": "[Filtered]", "dateOfBirth": "[Filtered]", "list": [{"apiKey": "[Filtered]", "ok": True, "x": None}], "empty": ""})

    def test_secret_object_keeps_its_keys(self):
        out = json.loads(fmt('{"card":{"brand":"visa","last4":"4242","meta":{"x":1},"none":null}}')["value"])
        self.assertEqual(out["card"], {"brand": "[Filtered]", "last4": "[Filtered]", "meta": {"x": "[Filtered]"}, "none": None})

    def test_numbers_and_text_as_javascript_writes_them(self):
        self.assertEqual(
            fmt('{"n":1.0,"m":1.5,"tiny":0.00001,"big":1e21,"neg":-0.0,"s":"héllo 😀 \\u2028 \\u0001"}')["value"],
            '{"n":1,"m":1.5,"tiny":0.00001,"big":1e+21,"neg":0,"s":"héllo 😀   \\u0001"}',
        )

    def test_inline_images_and_base64(self):
        out = json.loads(fmt(json.dumps({"img": IMAGE, "b": "QUJD" * 100 + "1", "name": "Ada"}), settings=body_settings("all"))["value"])
        self.assertEqual(out, {"img": "[image/png · 330 B]", "b": "[base64 · 300 B]", "name": "Ada"})

    def test_cut_json_is_redacted_loosely(self):
        out = fmt('{"token":"abc","avatar":"' + IMAGE, cut=True)
        self.assertEqual(out, {"value": '{"token":"[Filtered]","avatar":"[image/png · 330 B+]"', "truncated": True})

    def test_forms(self):
        self.assertEqual(fmt("user=ada&password=p4ss&code=123", "application/x-www-form-urlencoded")["value"], "user=ada&password=[Filtered]&code=123")

    def test_cap_and_truncated_flag(self):
        out = fmt('"' + "x" * 300 + '"', "text/plain")
        self.assertEqual(len(out["value"]), 200)
        self.assertTrue(out["truncated"])

    def test_empty_and_not_text(self):
        self.assertIsNone(fmt("   "))
        self.assertEqual(body_attributes("response", "x", "image/png", SETTINGS), {})
        self.assertTrue(is_textual(None) and is_textual("application/problem+json") and not is_textual("text/event-stream"))

    def test_labels(self):
        self.assertEqual([format_size(n) for n in (0, 1023, 1024, 1535, 1536, 1310720, 2359296, 5000000)], ["0 B", "1023 B", "1 KB", "1 KB", "2 KB", "1.3 MB", "2.3 MB", "4.8 MB"])
        self.assertEqual(describe_body("image/png", 3000), "[binary · image/png · 3 KB]")
        self.assertEqual(describe_body("text/event-stream; charset=utf-8"), "[event stream]")
        self.assertEqual(file_label("a.png", "image/png", 2048, True), "[file a.png · image/png · 2 KB+]")
        self.assertEqual(file_label(None, None, None), "[file blob]")
        self.assertEqual(unread_attributes("response", "application/json", 60)["http.response.body"], "[stream · application/json · 60 B]")

    def test_urls(self):
        self.assertEqual(
            redact_url("https://u:p@h.test/a?token=t&page=2&key=&session=s&sessions=3#frag?x=1"),
            "https://h.test/a?token=[Filtered]&page=2&key=&session=[Filtered]&sessions=3#frag?x=1",
        )
        self.assertEqual(redact_url("/p?sig=abc&q=1"), "/p?sig=[Filtered]&q=1")
        self.assertEqual(redact_query("pwd=x&api_key=y&state=z"), "pwd=[Filtered]&api_key=[Filtered]&state=[Filtered]")

    def test_url_matching(self):
        self.assertTrue(url_matches("https://api.example.com/x", ["api.example.com"]))
        self.assertTrue(url_matches("http://api.example.com:80/x", ["api.example.com"]))
        self.assertTrue(url_matches("http://localhost:3000/x", ["localhost:3000"]))
        self.assertTrue(url_matches("https://example.com/v2/a", ["https://example.com/v2"]))
        self.assertTrue(url_matches("https://x.test/api/a", [re.compile(r"/api/")]))
        self.assertFalse(url_matches("https://other.test/", ["api.example.com"]))


def upload(parts, boundary=b"b0und"):
    body = b""
    for part in parts:
        body += b"--" + boundary + b"\r\n" + part
    return body + b"--" + boundary + b"--\r\n"


UPLOAD = upload(
    [
        b'Content-Disposition: form-data; name="title"\r\n\r\nHoliday\r\n',
        b'Content-Disposition: form-data; name="avatar"; filename="me.png"\r\nContent-Type: image/png\r\n\r\n' + b"\x01" * 100000 + b"\r\n",
        b'Content-Disposition: form-data; name="tag"\r\n\r\na\r\n',
        b'Content-Disposition: form-data; name="tag"\r\n\r\nb\r\n',
    ]
)


class MultipartTest(unittest.TestCase):
    def test_any_chunking(self):
        for size in (1, 7, 64, 16384, len(UPLOAD)):
            reader = Multipart("b0und", 100)
            for i in range(0, len(UPLOAD), size):
                reader.push(UPLOAD[i : i + size])
            self.assertEqual(reader.result(), ('{"title":"Holiday","avatar":"[file me.png · image/png · 98 KB]","tag":["a","b"]}', False), size)

    def test_cut_short(self):
        reader = Multipart("b0und", 100)
        reader.push(UPLOAD[:5000])
        fields, cut = reader.result()
        self.assertTrue(cut)
        self.assertIn("me.png · image/png", fields)
        self.assertIn("+]", fields)

    def test_through_a_tap_with_secrets(self):
        body = upload([b'Content-Disposition: form-data; name="password"\r\n\r\nsecret\r\n', b'Content-Disposition: form-data; name="a"\r\n\r\nb\r\n'])
        out = bytes_attributes("request", body, "multipart/form-data; boundary=b0und", None, SETTINGS)
        self.assertEqual(out["http.request.body"], '{"password":"[Filtered]","a":"b"}')


class TapTest(unittest.TestCase):
    def test_text_gzip_and_binary(self):
        data = json.dumps({"error": "x", "token": "t"}).encode()
        for encoding, raw in ((None, data), ("gzip", gzip.compress(data)), ("deflate", zlib.compress(data))):
            out = bytes_attributes("response", raw, "application/json", encoding, SETTINGS)
            self.assertEqual(out["http.response.body"], '{"error":"x","token":"[Filtered]"}', encoding)
        self.assertEqual(bytes_attributes("response", b"\x00" * 3000, "image/png", None, SETTINGS)["http.response.body"], "[binary · image/png · 3 KB]")

    def test_raw_limit(self):
        tap = Tap(SETTINGS, lambda: "text/plain")
        tap.add(b"y" * (200 * 1024))
        self.assertTrue(tap.cut)
        self.assertEqual(tap.total, 200 * 1024)
        out = side_attributes("request", tap, "text/plain", None, SETTINGS)
        self.assertEqual(len(out["http.request.body"]), 200)
        self.assertTrue(out["http.request.body.truncated"])

    def test_tee_keeps_what_the_app_reads_and_drains_the_rest(self):
        import io

        tap = Tap(SETTINGS, lambda: "text/plain")
        tee = TeeInput(io.BytesIO(b"hello world"), tap)
        self.assertEqual(tee.read(5), b"hello")
        buffer = bytearray(3)
        self.assertEqual(tee.readinto(buffer), 3)
        tee.drain(11, SETTINGS)
        self.assertEqual(tap.text(None), "hello world")


if __name__ == "__main__":
    unittest.main()
