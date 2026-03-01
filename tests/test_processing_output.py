from app.processing import _build_results_text, _build_timestamps


def test_processing_uses_numero_sign_in_output():
    results = [
        {
            "time": 3.0,
            "label": "№86 Мусиенко Мария",
            "num": "86",
            "name": "Мусиенко Мария",
            "time_text": "00:03",
        }
    ]

    assert _build_results_text(results) == "00:00 Начало трансляции\n00:03 №86 Мусиенко Мария"
    assert _build_timestamps(results) == [{"time": 3.0, "label": "№86 Мусиенко Мария"}]
