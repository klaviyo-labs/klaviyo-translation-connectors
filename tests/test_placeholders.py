from klaviyo_tc.core import placeholders

PATTERN = r"\{\{[^}]+\}\}|\{%[^%]+%\}"


def test_identical_strings_match():
    assert placeholders.matches("Hi {{ first_name }}!", "Bonjour {{ first_name }} !", PATTERN)


def test_reordered_curly_placeholders_still_match():
    source = "{{ a }} and {{ b }}"
    translation = "{{ b }} et {{ a }}"
    assert placeholders.matches(source, translation, PATTERN)


def test_duplicated_curly_placeholder_mismatch():
    source = "{{ x }} once"
    translation = "{{ x }} {{ x }} deux fois"
    assert not placeholders.matches(source, translation, PATTERN)


def test_missing_curly_placeholder_mismatch():
    source = "Hello {{ first_name }}"
    translation = "Bonjour"
    assert not placeholders.matches(source, translation, PATTERN)


def test_percent_tags_must_keep_order():
    source = "{% if a %}A{% endif %}"
    reordered = "{% endif %}A{% if a %}"
    assert not placeholders.matches(source, reordered, PATTERN)


def test_percent_tags_same_order_match():
    source = "{% if a %}A{% endif %}"
    translation = "{% if a %}Ah{% endif %}"
    assert placeholders.matches(source, translation, PATTERN)


def test_whitespace_inside_tokens_is_normalized():
    source = "{{   first_name  }}"
    translation = "{{ first_name }}"
    assert placeholders.matches(source, translation, PATTERN)
