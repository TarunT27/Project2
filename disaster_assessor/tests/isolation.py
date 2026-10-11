"""Import first in any test touching Claude: hides the developer's real credentials.

Removes API keys/tokens from the environment and points the SDK at an empty config dir, so an
`ant auth login` profile on this machine can never make a test call the real (billed) API.
"""

import os
import tempfile

for _name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
    os.environ.pop(_name, None)
os.environ["ANTHROPIC_CONFIG_DIR"] = tempfile.mkdtemp(prefix="dra-test-anthropic-")
