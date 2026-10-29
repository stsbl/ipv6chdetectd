LOCALE_DISABLE_POOTLE_DOWNLOAD=1

TEST_TARGETS += ipv6_hook_tests

.PHONY: ipv6_hook_tests
ipv6_hook_tests:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
