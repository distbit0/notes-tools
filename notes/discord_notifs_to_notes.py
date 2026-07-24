import sys


DISABLED_MESSAGE = "Discord notification polling is disabled."


def main() -> int:
    print(DISABLED_MESSAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
