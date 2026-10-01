"""Config and options flow for the E.ON W1000 integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_EMAIL_SENDER,
    CONF_EMAIL_SUBJECT,
    CONF_IMAP_HOST,
    CONF_IMAP_PASS,
    CONF_IMAP_PORT,
    CONF_IMAP_USER,
    CONF_INITIAL_EXPORT,
    CONF_INITIAL_IMPORT,
    CONF_POLL_INTERVAL,
    CONF_SEARCH_DAYS,
    DEFAULT_EMAIL_SENDER,
    DEFAULT_EMAIL_SUBJECT,
    DEFAULT_IMAP_PORT,
    DEFAULT_INITIAL_EXPORT,
    DEFAULT_INITIAL_IMPORT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_SEARCH_DAYS,
    DOMAIN,
)
from .imap_client import ImapClient

# Every imap_* code returned by ImapClient.test_connection() maps to the same
# error key in strings.json.  The previous flow tried to detect a failed login
# by searching the *localised* exception text for an English word, so on a
# Hungarian Home Assistant a wrong password was reported as "unknown error".
_ERROR_CODES = {"imap_auth", "imap_connect"}


def _schema(defaults: dict[str, Any], *, with_password: bool = True) -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Required(CONF_IMAP_HOST, default=defaults.get(CONF_IMAP_HOST, "")): TextSelector(),
        vol.Required(
            CONF_IMAP_PORT, default=defaults.get(CONF_IMAP_PORT, DEFAULT_IMAP_PORT)
        ): NumberSelector(
            NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
        ),
        vol.Required(CONF_IMAP_USER, default=defaults.get(CONF_IMAP_USER, "")): TextSelector(),
    }
    if with_password:
        fields[vol.Required(CONF_IMAP_PASS)] = TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
    fields[
        vol.Optional(
            CONF_POLL_INTERVAL, default=defaults.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        )
    ] = NumberSelector(NumberSelectorConfig(min=5, max=1440, mode=NumberSelectorMode.BOX))
    fields[
        vol.Optional(CONF_SEARCH_DAYS, default=defaults.get(CONF_SEARCH_DAYS, DEFAULT_SEARCH_DAYS))
    ] = NumberSelector(NumberSelectorConfig(min=1, max=60, mode=NumberSelectorMode.BOX))
    fields[
        vol.Optional(
            CONF_EMAIL_SENDER, default=defaults.get(CONF_EMAIL_SENDER, DEFAULT_EMAIL_SENDER)
        )
    ] = TextSelector()
    fields[
        vol.Optional(
            CONF_EMAIL_SUBJECT, default=defaults.get(CONF_EMAIL_SUBJECT, DEFAULT_EMAIL_SUBJECT)
        )
    ] = TextSelector()
    return vol.Schema(fields)


def _normalise(user_input: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (value.strip() if isinstance(value, str) else value)
        for key, value in user_input.items()
    }


async def _validate(hass, user_input: dict[str, Any]) -> str | None:
    """Return an error code, or ``None`` when the mailbox is reachable."""
    client = ImapClient(
        host=user_input[CONF_IMAP_HOST],
        port=int(user_input[CONF_IMAP_PORT]),
        username=user_input[CONF_IMAP_USER],
        password=user_input[CONF_IMAP_PASS],
        sender_filter=user_input.get(CONF_EMAIL_SENDER, DEFAULT_EMAIL_SENDER),
        subject_filter=user_input.get(CONF_EMAIL_SUBJECT, DEFAULT_EMAIL_SUBJECT),
    )
    ok, code = await hass.async_add_executor_job(client.test_connection)
    return None if ok else code


class EonW1000ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup."""

    # 2.0 adds the two bootstrap keys to the entry data (the schema itself is
    # unchanged), so the version stays at 2 and ``async_migrate_entry`` brings a
    # 1.x entry over.  Home Assistant tolerates a *minor* mismatch without a
    # handler but a *major* one needs it, and without it the entry refuses to
    # load entirely ("Migration handler not found").
    VERSION = 2
    MINOR_VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = _normalise(user_input)
            unique_id = f"{user_input[CONF_IMAP_HOST]}:{user_input[CONF_IMAP_USER]}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()
            error = await _validate(self.hass, user_input)
            if error is None:
                return self.async_create_entry(
                    title="E.ON W1000", data={**user_input, **self._bootstrap_defaults()}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input or {}),
            errors=errors,
            description_placeholders={
                "portal_url": "https://e-portal.eon-hungaria.com/w1000",
                "subject": DEFAULT_EMAIL_SUBJECT,
            },
        )

    @staticmethod
    def _bootstrap_defaults() -> dict[str, float]:
        # Kept only so an existing entry keeps its keys; the cumulative series is
        # anchored to the recorder, never seeded from these values.
        return {
            CONF_INITIAL_IMPORT: DEFAULT_INITIAL_IMPORT,
            CONF_INITIAL_EXPORT: DEFAULT_INITIAL_EXPORT,
        }

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EonW1000OptionsFlow()


class EonW1000OptionsFlow(OptionsFlow):
    """Change the poll interval, search window, filters or the mailbox password."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            user_input = _normalise(user_input)
            # An empty password field means "keep the stored one".
            if not user_input.get(CONF_IMAP_PASS):
                user_input.pop(CONF_IMAP_PASS, None)
            merged = {**current, **user_input}
            error = await _validate(self.hass, merged)
            if error is None:
                return self.async_create_entry(data=user_input)
            errors["base"] = error
        return self.async_show_form(
            step_id="init",
            data_schema=self._schema_with_optional_password(current),
            errors=errors,
        )

    @staticmethod
    def _schema_with_optional_password(defaults: dict[str, Any]) -> vol.Schema:
        schema = _schema(defaults, with_password=False)
        return schema.extend(
            {
                vol.Optional(CONF_IMAP_PASS): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                )
            }
        )
