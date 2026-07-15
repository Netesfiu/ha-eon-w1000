"""Config flow for E.ON W1000 integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

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
    DEFAULT_EMAIL_SENDER,
    DEFAULT_EMAIL_SUBJECT,
    DEFAULT_IMAP_PORT,
    DEFAULT_INITIAL_EXPORT,
    DEFAULT_INITIAL_IMPORT,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)
from .imap_client import ImapClient

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_IMAP_HOST): str,
        vol.Required(CONF_IMAP_PORT, default=DEFAULT_IMAP_PORT): int,
        vol.Required(CONF_IMAP_USER): str,
        vol.Required(CONF_IMAP_PASS): str,
        vol.Optional(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): int,
        vol.Optional(CONF_EMAIL_SENDER, default=DEFAULT_EMAIL_SENDER): str,
        vol.Optional(CONF_EMAIL_SUBJECT, default=DEFAULT_EMAIL_SUBJECT): str,
        vol.Optional(
            CONF_INITIAL_IMPORT,
            default=DEFAULT_INITIAL_IMPORT,
        ): vol.Coerce(float),
        vol.Optional(
            CONF_INITIAL_EXPORT,
            default=DEFAULT_INITIAL_EXPORT,
        ): vol.Coerce(float),
    }
)


class EonW1000ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for E.ON W1000."""

    VERSION = 1
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return EonW1000OptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Test IMAP connection
            try:
                client = ImapClient(
                    host=user_input[CONF_IMAP_HOST],
                    port=user_input[CONF_IMAP_PORT],
                    username=user_input[CONF_IMAP_USER],
                    password=user_input[CONF_IMAP_PASS],
                )
                ok, msg = await self.hass.async_add_executor_job(
                    client.test_connection
                )
                if not ok:
                    if "auth" in msg.lower() or "login" in msg.lower():
                        errors["base"] = "imap_auth"
                    else:
                        errors["base"] = "imap_connect"
                    _LOGGER.warning("IMAP test failed: %s", msg)
            except Exception as exc:
                _LOGGER.exception("IMAP test exception")
                errors["base"] = "imap_connect"
            else:
                # Check for duplicate entries
                self._async_abort_entries_match(
                    {
                        CONF_IMAP_HOST: user_input[CONF_IMAP_HOST],
                        CONF_IMAP_USER: user_input[CONF_IMAP_USER],
                    }
                )

                return self.async_create_entry(
                    title=f"E.ON W1000 ({user_input[CONF_IMAP_USER]})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )


class EonW1000OptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for E.ON W1000."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self._config_entry.options
        data = self._config_entry.data

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_IMAP_HOST,
                    default=options.get(CONF_IMAP_HOST, data.get(CONF_IMAP_HOST, "")),
                ): str,
                vol.Required(
                    CONF_IMAP_PORT,
                    default=options.get(CONF_IMAP_PORT, data.get(CONF_IMAP_PORT, DEFAULT_IMAP_PORT)),
                ): int,
                vol.Required(
                    CONF_IMAP_USER,
                    default=options.get(CONF_IMAP_USER, data.get(CONF_IMAP_USER, "")),
                ): str,
                vol.Required(
                    CONF_IMAP_PASS,
                    default=options.get(CONF_IMAP_PASS, data.get(CONF_IMAP_PASS, "")),
                ): str,
                vol.Optional(
                    CONF_POLL_INTERVAL,
                    default=options.get(CONF_POLL_INTERVAL, data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)),
                ): int,
                vol.Optional(
                    CONF_EMAIL_SENDER,
                    default=options.get(CONF_EMAIL_SENDER, data.get(CONF_EMAIL_SENDER, DEFAULT_EMAIL_SENDER)),
                ): str,
                vol.Optional(
                    CONF_EMAIL_SUBJECT,
                    default=options.get(CONF_EMAIL_SUBJECT, data.get(CONF_EMAIL_SUBJECT, DEFAULT_EMAIL_SUBJECT)),
                ): str,
                vol.Optional(
                    CONF_INITIAL_IMPORT,
                    default=options.get(CONF_INITIAL_IMPORT, data.get(CONF_INITIAL_IMPORT, DEFAULT_INITIAL_IMPORT)),
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_INITIAL_EXPORT,
                    default=options.get(CONF_INITIAL_EXPORT, data.get(CONF_INITIAL_EXPORT, DEFAULT_INITIAL_EXPORT)),
                ): vol.Coerce(float),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
