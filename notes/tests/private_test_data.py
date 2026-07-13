import json
from pathlib import Path


PRIVATE_TEST_DATA_PATH = Path(__file__).with_name("private_test_data.json")
try:
    PRIVATE_TEST_DATA = json.loads(PRIVATE_TEST_DATA_PATH.read_text(encoding="utf-8"))
except FileNotFoundError as error:
    raise RuntimeError(
        f"Private test data is required at {PRIVATE_TEST_DATA_PATH}"
    ) from error
