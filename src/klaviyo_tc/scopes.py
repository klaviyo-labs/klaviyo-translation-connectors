"""Resolve --campaign/--flow/--tag/--template/--universal-content/--all/--id scope
selectors to translation ids.

Campaigns resolve through the GA Campaigns API (message id == variation id),
falling back to beta omni traversal; see klaviyo.py's module docstring.
Templates and universal content use Klaviyo's GA Templates API.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .core.concurrency import map_ordered
from .klaviyo import KlaviyoAPIError, KlaviyoClient

CAMPAIGN_VARIATION = "campaign-variation"
FLOW_MESSAGE = "flow-message"
TEMPLATE = "template"
TEMPLATE_UNIVERSAL_CONTENT = "template-universal-content"
SINGLE_HTML_BODY_EDITOR_TYPES = {"CODE", "SIMPLE"}


class ScopeError(Exception):
    pass


@dataclass
class ResolvedResource:
    resource_type: str  # CAMPAIGN_VARIATION | FLOW_MESSAGE | TEMPLATE | TEMPLATE_UNIVERSAL_CONTENT
    resource_id: str
    channel: str | None
    parent_name: str  # owning campaign/flow/template name, for job naming and reporting
    editor_type: str | None = None  # templates only: CODE/SIMPLE store one HTML body value


@dataclass
class ScopeItem:
    resource: ResolvedResource
    outcome: str  # "resolved" | "created" | "would_create" | "no_translation" | "error"
    translation_id: str | None = None
    detail: str | None = None
    single_html_body: bool = False


@dataclass
class ScopeResult:
    description: str
    items: list[ScopeItem] = field(default_factory=list)

    @property
    def translation_ids(self) -> list[str]:
        seen: set[str] = set()
        ids: list[str] = []
        for item in self.items:
            if item.translation_id and item.translation_id not in seen:
                seen.add(item.translation_id)
                ids.append(item.translation_id)
        return ids

    @property
    def single_html_body_items(self) -> list[ScopeItem]:
        return [item for item in self.items if item.single_html_body]


def _message_channel(message: dict) -> str | None:
    attrs = message.get("attributes", {}) or {}
    channel = attrs.get("channel") or (attrs.get("definition") or {}).get("channel")
    # Flow messages report "Email"; the Translations API only accepts lowercase channels.
    return channel.lower() if channel else None


def resolve_campaign(klaviyo: KlaviyoClient, campaign_id: str) -> tuple[str, list[ResolvedResource]]:
    try:
        name = klaviyo.get_campaign(campaign_id)["attributes"]["name"]
    except KlaviyoAPIError as exc:
        if exc.status_code != 404:
            raise
        return _resolve_omni_campaign(klaviyo, campaign_id)
    resources = [
        ResolvedResource(CAMPAIGN_VARIATION, message["id"], _message_channel(message), name)
        for message in klaviyo.list_campaign_messages(campaign_id)
    ]
    return name, resources


def _resolve_omni_campaign(klaviyo: KlaviyoClient, campaign_id: str) -> tuple[str, list[ResolvedResource]]:
    name = klaviyo.get_campaign(campaign_id, omni=True)["attributes"]["name"]
    resources = []
    for message in klaviyo.list_campaign_messages(campaign_id, omni=True):
        channel = _message_channel(message)
        for variation in klaviyo.list_campaign_variations(message["id"]):
            resources.append(ResolvedResource(CAMPAIGN_VARIATION, variation["id"], channel, name))
    return name, resources


def resolve_flow(klaviyo: KlaviyoClient, flow_id: str) -> tuple[str, list[ResolvedResource]]:
    name = klaviyo.get_flow(flow_id)["attributes"]["name"]
    resources = []
    for action in klaviyo.list_flow_actions(flow_id):
        for message in klaviyo.list_flow_messages(action["id"]):
            channel = _message_channel(message)
            resources.append(ResolvedResource(FLOW_MESSAGE, message["id"], channel, name))
    return name, resources


def resolve_template(klaviyo: KlaviyoClient, template_id: str, *, channel: str = "email") -> tuple[str, list[ResolvedResource]]:
    attrs = klaviyo.get_template(template_id)["attributes"]
    resource = ResolvedResource(TEMPLATE, template_id, channel, attrs["name"], editor_type=attrs.get("editor_type"))
    return attrs["name"], [resource]


def resolve_templates(
    klaviyo: KlaviyoClient, *, name_contains: str | None = None, updated_since: str | None = None, channel: str = "email"
) -> list[ResolvedResource]:
    resources = []
    for item in klaviyo.list_templates(name_contains=name_contains, updated_since=updated_since):
        attrs = item.get("attributes", {})
        resources.append(ResolvedResource(TEMPLATE, item["id"], channel, attrs.get("name", item["id"]), editor_type=attrs.get("editor_type")))
    return resources


def resolve_universal_content_item(klaviyo: KlaviyoClient, uc_id: str) -> tuple[str, list[ResolvedResource]]:
    attrs = klaviyo.get_universal_content_item(uc_id)["attributes"]
    resource = ResolvedResource(TEMPLATE_UNIVERSAL_CONTENT, uc_id, "email", attrs["name"])
    return attrs["name"], [resource]


def resolve_all_universal_content(klaviyo: KlaviyoClient, *, name_contains: str | None = None) -> list[ResolvedResource]:
    resources = []
    for item in klaviyo.list_universal_content(name_contains=name_contains):
        attrs = item.get("attributes", {})
        resources.append(ResolvedResource(TEMPLATE_UNIVERSAL_CONTENT, item["id"], "email", attrs.get("name", item["id"])))
    return resources


def resolve_tag(klaviyo: KlaviyoClient, tag_name: str) -> list[ResolvedResource]:
    tags = klaviyo.find_tags_by_name(tag_name)
    if not tags:
        raise ScopeError(f"no tag named '{tag_name}' found")
    if len(tags) > 1:
        raise ScopeError(f"tag name '{tag_name}' is ambiguous ({len(tags)} tags matched)")
    tag_id = tags[0]["id"]

    resources = []
    for campaign_id in klaviyo.list_tag_campaign_ids(tag_id):
        _, campaign_resources = resolve_campaign(klaviyo, campaign_id)
        resources.extend(campaign_resources)
    for flow_id in klaviyo.list_tag_flow_ids(tag_id):
        _, flow_resources = resolve_flow(klaviyo, flow_id)
        resources.extend(flow_resources)
    return resources


def _resolve_translation(
    klaviyo: KlaviyoClient, resource: ResolvedResource, *, create_missing: bool, dry_run: bool, config
) -> ScopeItem:
    single_html_body = resource.editor_type in SINGLE_HTML_BODY_EDITOR_TYPES

    def item(outcome: str, *, translation_id: str | None = None, detail: str | None = None) -> ScopeItem:
        return ScopeItem(resource, outcome, translation_id=translation_id, detail=detail, single_html_body=single_html_body)

    try:
        translation = klaviyo.find_translation_for_resource(resource.resource_id)
    except KlaviyoAPIError as exc:
        return item("error", detail=str(exc))

    if translation is not None:
        return item("resolved", translation_id=translation["id"])

    if not create_missing:
        return item("no_translation")
    if dry_run:
        return item("would_create")

    try:
        created = klaviyo.create_translation(
            channel=resource.channel,
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
            source_locale=config.klaviyo.source_locale,
            target_locales=list(config.locales.keys()),
            fallback_locale=config.klaviyo.fallback_locale,
        )
        return item("created", translation_id=created["id"])
    except KlaviyoAPIError as exc:
        if exc.status_code == 409:
            # Someone else created it between our lookup and our create; adopt it.
            existing = klaviyo.find_translation_for_resource(resource.resource_id)
            if existing is not None:
                return item("resolved", translation_id=existing["id"])
        return item("error", detail=str(exc))


def resolve_scope(
    klaviyo: KlaviyoClient,
    *,
    campaign_ids: list[str] | None = None,
    flow_ids: list[str] | None = None,
    tag_name: str | None = None,
    template_ids: list[str] | None = None,
    templates_all: bool = False,
    universal_content_ids: list[str] | None = None,
    all_universal_content: bool = False,
    name_contains: str | None = None,
    updated_since: str | None = None,
    template_channel: str = "email",
    create_missing: bool,
    config,
    dry_run: bool = False,
) -> ScopeResult:
    resources: list[ResolvedResource] = []
    names: list[str] = []

    for campaign_id in campaign_ids or []:
        name, campaign_resources = resolve_campaign(klaviyo, campaign_id)
        names.append(name)
        resources.extend(campaign_resources)

    for flow_id in flow_ids or []:
        name, flow_resources = resolve_flow(klaviyo, flow_id)
        names.append(name)
        resources.extend(flow_resources)

    if tag_name:
        resources.extend(resolve_tag(klaviyo, tag_name))
        names.append(tag_name)

    for template_id in template_ids or []:
        name, template_resources = resolve_template(klaviyo, template_id, channel=template_channel)
        names.append(name)
        resources.extend(template_resources)

    if templates_all:
        resources.extend(
            resolve_templates(klaviyo, name_contains=name_contains, updated_since=updated_since, channel=template_channel)
        )
        names.append("templates")

    for uc_id in universal_content_ids or []:
        name, uc_resources = resolve_universal_content_item(klaviyo, uc_id)
        names.append(name)
        resources.extend(uc_resources)

    if all_universal_content:
        resources.extend(resolve_all_universal_content(klaviyo, name_contains=name_contains))
        names.append("universal content")

    description = ", ".join(names) if names else "?"
    resolved = map_ordered(
        lambda resource: _resolve_translation(
            klaviyo, resource, create_missing=create_missing, dry_run=dry_run, config=config
        ),
        resources,
        config.klaviyo.concurrency,
    )
    items = [
        item if exc is None else ScopeItem(resource, "error", detail=str(exc))
        for resource, item, exc in resolved
    ]
    return ScopeResult(description=description, items=items)
