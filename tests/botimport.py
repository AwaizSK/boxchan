"""Import bot.py hermetically: temp state/config, no token, and no .env loading.

bot.py calls load_dotenv() at import; that would pull a developer's real token from the repo
.env into the test process (and a malformed one would SystemExit the import). The loader is
patched out for the duration of the import. Import order matters: this module first.
"""
import os
import sys
import tempfile
from unittest import mock

TMP = tempfile.mkdtemp(prefix="tts-bot-test-")
for _k in ("DISCORD_TOKEN", "CREDENTIALS_DIRECTORY", "TTS_CHANNEL_ID"):
    os.environ.pop(_k, None)
os.environ["DISCORD_TOKEN_FILE"] = os.path.join(TMP, "no-such-token")
os.environ["STATE_FILE"] = os.path.join(TMP, "state.json")
os.environ["CONFIG_FILE"] = os.path.join(TMP, "config.json")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dotenv  # noqa: E402

with mock.patch.object(dotenv, "load_dotenv", return_value=False) as _ld:
    import bot  # noqa: E402

LOAD_DOTENV_CALLS = _ld.call_count
