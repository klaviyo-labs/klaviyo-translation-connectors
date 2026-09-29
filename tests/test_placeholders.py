from klaviyo_tc.core import placeholders


def test_identical_strings_match():
    assert placeholders.matches("Hi {{ first_name }}!", "Bonjour {{ first_name }} !")


def test_reordered_curly_placeholders_still_match():
    source = "{{ a }} and {{ b }}"
    translation = "{{ b }} et {{ a }}"
    assert placeholders.matches(source, translation)


def test_duplicated_curly_placeholder_mismatch():
    source = "{{ x }} once"
    translation = "{{ x }} {{ x }} deux fois"
    assert not placeholders.matches(source, translation)


def test_missing_curly_placeholder_mismatch():
    source = "Hello {{ first_name }}"
    translation = "Bonjour"
    assert not placeholders.matches(source, translation)


def test_percent_tags_must_keep_order():
    source = "{% if a %}A{% endif %}"
    reordered = "{% endif %}A{% if a %}"
    assert not placeholders.matches(source, reordered)


def test_percent_tags_same_order_match():
    source = "{% if a %}A{% endif %}"
    translation = "{% if a %}Ah{% endif %}"
    assert placeholders.matches(source, translation)


def test_whitespace_inside_tokens_is_normalized():
    source = "{{   first_name  }}"
    translation = "{{ first_name }}"
    assert placeholders.matches(source, translation)


def test_percent_sign_inside_quoted_string_does_not_close_tag():
    # The "%}" inside the quoted "10%" must not be mistaken for the tag close.
    text = '{% if discount == "10%" %}'
    curly, percent = placeholders.signature(text)
    assert percent == ['{% if discount == "10%" %}']


def test_curly_brace_inside_quoted_string_does_not_close_tag():
    # The quoted "}" must not be mistaken for the start of the "}}" close.
    text = '{{ value|default:"}" }}'
    curly, percent = placeholders.signature(text)
    assert list(curly.elements()) == ['{{ value|default:"}" }}']


def test_quoted_content_with_percent_tag_matches_when_identical():
    text = '{% if discount == "10%" %}Sale{% endif %}'
    assert placeholders.matches(text, text)


def test_quoted_content_with_curly_tag_matches_when_identical():
    text = '{{ value|default:"}" }} rest'
    assert placeholders.matches(text, text)


def test_whitespace_inside_quotes_is_preserved_not_normalized():
    # Two spaces inside the quoted literal must not collapse to one.
    source = '{{ value|default:"a  b" }}'
    translation_same = '{{ value|default:"a  b" }}'
    translation_collapsed = '{{ value|default:"a b" }}'
    assert placeholders.matches(source, translation_same)
    assert not placeholders.matches(source, translation_collapsed)


def test_whitespace_outside_quotes_is_still_normalized():
    source = '{%  if   discount == "10%"  %}'
    translation = '{% if discount == "10%" %}'
    assert placeholders.matches(source, translation)
