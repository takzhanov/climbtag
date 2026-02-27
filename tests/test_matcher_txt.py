from app.matcher import ProtocolMatcher


def test_matcher_loads_plain_text_protocol(tmp_path):
    protocol = tmp_path / "participants.txt"
    protocol.write_text(
        "5;Сидорова Женя\n"
        "№6 Красова Анна\n"
        "#7-Иванова Мария\n",
        encoding="utf-8",
    )

    matcher = ProtocolMatcher(protocol)

    assert matcher.find_participant("5") == ("5", "Сидорова Женя")
    assert matcher.find_participant("6") == ("6", "Красова Анна")
    assert matcher.find_participant("7") == ("7", "Иванова Мария")
