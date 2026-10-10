from fyos_plugin_betrieb.json5 import laden


def test_json5_openclaw_formen():
    text = """
    {
      // Kommentar
      meta: { version: "2026.9.5", ok: true, leer: null },   /* block */
      models: {
        providers: {
          modell: { baseUrl: "http://127.0.0.1:18422/v1", timeoutSeconds: 900,
                  models: [ { id: 'modell', note: 'sagt "hi"', url: "http://x//y" }, ], },
        },
      },
      liste: [1, 2.5, -3,],
    }
    """
    d = laden(text)
    assert d["meta"] == {"version": "2026.9.5", "ok": True, "leer": None}
    assert d["models"]["providers"]["modell"]["models"][0] == {"id": "modell", "note": 'sagt "hi"', "url": "http://x//y"}
    assert d["liste"] == [1, 2.5, -3]
