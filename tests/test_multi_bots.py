from pathlib import Path

from tools.multi_bots import REPO, instance_env, looks_like_token, main_port, parse_tokens, read_env_file

FAKE = "MTA" + "x" * 23 + ".AbCdEf." + "y" * 30


def test_parse_tokens_skips_comments_blanks_and_duplicates():
    text = "# Kopf\n\nA.B.C\n  D.E.F  # bot2\nA.B.C\n"
    assert parse_tokens(text) == ["A.B.C", "D.E.F"]


def test_looks_like_token():
    assert looks_like_token(FAKE)
    assert not looks_like_token("k3jH8sdf7Gh2kLm9Qw1Er5Ty6Ui8Op0A")   # Client Secret: kein Punkt


def test_read_env_file_strips_quotes(tmp_path):
    env = tmp_path / ".env"
    env.write_text('# x\nA=1\nB="zwei"\nC = \'drei\'\nkaputt\n', encoding="utf-8")
    assert read_env_file(env) == {"A": "1", "B": "zwei", "C": "drei"}
    assert read_env_file(tmp_path / "fehlt") == {}


def test_main_port_default_and_invalid():
    assert main_port({}) == 8765
    assert main_port({"DM_BRIDGE_PORT": "9000"}) == 9000
    assert main_port({"DM_BRIDGE_PORT": "abc"}) == 8765


def test_instance_env_overrides_only_instance_values():
    repo_env = {"DISCORD_TOKEN": "haupt", "LANGUAGE": "de", "VOICE_CONTROL": "true",
                "DM_BRIDGE_HOST": "0.0.0.0", "YDL_COOKIES_FILE": "cookies.txt"}
    base_env = {"PATH": "p", "MUSICBOT_LOG_FILE": "x.log"}
    env = instance_env(base_env, repo_env, FAKE, 8767)
    assert env["DISCORD_TOKEN"] == FAKE
    assert env["DM_BRIDGE_PORT"] == "8767"
    assert env["DM_BRIDGE_HOST"] == "127.0.0.1"
    assert env["VOICE_CONTROL"] == "false"
    assert env["LANGUAGE"] == "de"          # Rest der Haupt-.env wird geerbt
    assert env["PATH"] == "p"
    assert "MUSICBOT_LOG_FILE" not in env
    assert Path(env["YDL_COOKIES_FILE"]) == REPO / "cookies.txt"


def test_instance_env_keeps_absolute_cookie_path(tmp_path):
    cookies = str(tmp_path / "c.txt")
    env = instance_env({}, {"YDL_COOKIES_FILE": cookies}, FAKE, 8766)
    assert env["YDL_COOKIES_FILE"] == cookies
