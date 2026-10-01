"""Source-only history: synthetic workbooks, no legacy Recorder anchors."""
import asyncio
from datetime import datetime
import pytest
from test_core import write_export, TZ
from custom_components.eon_w1000 import const, hourly, parser


def test_source_backfill_has_zero_boundary_and_independent_chains(tmp_path):
    parsed = parser.parse_eon_xlsx(str(write_export(tmp_path / 'source.xlsx', days=2)), TZ)
    selection = hourly.select_run(parsed.hours, 4)
    result = hourly.source_history(selection)
    assert const.STATISTIC_IMPORT_ID == 'sensor.eon_w1000_eon_w1000_grid_import'
    assert const.STATISTIC_EXPORT_ID == 'sensor.eon_w1000_eon_w1000_grid_export'
    assert const.STORAGE_KEY == 'eon_w1000_excel_history'
    for rows, channel in [(result.import_rows, 'ap'), (result.export_rows, 'am')]:
        assert rows[0] == {'start': selection.anchor_hour.isoformat(), 'state': 0.0, 'sum': 0.0}
        assert len(rows) == 49
        for previous, row, hour in zip(rows, rows[1:], selection.hours):
            assert round((row['sum'] - previous['sum']) * 1000) == round(getattr(hour, channel) * 1000)
    assert result.import_rows != result.export_rows
    assert hourly.source_history(selection) == result


def test_coordinator_imports_source_files_without_recorder_anchor(tmp_path):
    from unittest.mock import AsyncMock
    from custom_components.eon_w1000.coordinator import EonW1000Coordinator
    from homeassistant.core import HomeAssistant

    async def exercise():
        hass = HomeAssistant(str(tmp_path))
        from homeassistant.helpers import frame
        frame.async_setup(hass)
        coordinator = EonW1000Coordinator(hass, {})
        coordinator._push = AsyncMock()
        coordinator._async_save = AsyncMock()
        first = write_export(tmp_path / 'first.xlsx', days=1)
        second = write_export(tmp_path / 'second.xlsx', start=datetime(2026, 9, 24), days=1)
        payload = await coordinator.async_import_files([str(first), str(second)])
        assert payload['status'] == 'ok'
        # the archive's raw registers and parse bookkeeping travel with the
        # backfill exactly as they do on the mail path
        assert payload['raw_m180'] is not None and payload['raw_m280'] is not None
        assert payload['parse_failures'] == 0
        calls = coordinator._push.call_args_list
        assert [c.args[0] for c in calls] == [const.STATISTIC_IMPORT_ID, const.STATISTIC_EXPORT_ID]
        original = [c.args[1] for c in calls]
        assert len(original[0]) == 49
        coordinator._push.reset_mock()
        await coordinator.async_import_files([str(second)])
        assert [c.args[1] for c in coordinator._push.call_args_list] == original
        gap = write_export(tmp_path / 'gap.xlsx', start=datetime(2026, 9, 27), days=1)
        coordinator._push.reset_mock()
        result = await coordinator.async_import_files([str(gap)])
        assert result['status'] == 'no_data'
        coordinator._push.assert_not_called()
        assert 'gap' in result['last_error']
        broken = tmp_path / 'broken.xlsx'
        broken.write_bytes(b'not an xlsx')
        result = await coordinator.async_import_files([str(broken)])
        assert result['status'] == 'no_data' and result['parse_failures'] == 1
        assert 'broken.xlsx' in (result['last_error'] or '')
        await hass.async_stop()
    asyncio.run(exercise())


def test_old_wide_is_explicitly_excluded(tmp_path):
    import openpyxl
    book = openpyxl.Workbook()
    book.active.append(['POD', 'Változó', 'Időbélyeg', 'Mértékegység', 'Érték'])
    book.active.append(['synthetic', '+A', datetime(2025, 1, 1), 'kWh', 1])
    path = tmp_path / 'old.xlsx'
    book.save(path)
    with pytest.raises(ValueError, match='old_wide.*not supported'):
        parser.parse_eon_xlsx(str(path), TZ)


def test_dashboard_sensor_is_history_only_without_live_numeric_samples(tmp_path):
    from types import SimpleNamespace
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import frame
    from custom_components.eon_w1000.coordinator import EonW1000Coordinator
    from custom_components.eon_w1000.sensor import EonW1000EnergySensor
    async def exercise():
        hass = HomeAssistant(str(tmp_path))
        frame.async_setup(hass)
        coordinator = EonW1000Coordinator(hass, {})
        coordinator.data = {'latest_import': 123.45, 'latest_export': 45.67}
        for key, value, identity in [('grid_import', 123.45, const.STATISTIC_IMPORT_ID), ('grid_export', 45.67, const.STATISTIC_EXPORT_ID)]:
            sensor = EonW1000EnergySensor(coordinator, SimpleNamespace(entry_id='synthetic'), key)
            assert sensor.state_class == 'total'
            assert sensor.entity_category is None
            assert sensor.native_value is None  # never create processing-time deltas
            assert sensor.extra_state_attributes['historical_total'] == value
            assert sensor.extra_state_attributes['statistic_id'] == identity
        await hass.async_stop()
    asyncio.run(exercise())

def test_import_files_service_is_registered_with_its_schema(tmp_path):
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import frame
    import custom_components.eon_w1000 as component
    from custom_components.eon_w1000 import coordinator as coordinator_module

    async def exercise():
        hass = HomeAssistant(str(tmp_path))
        frame.async_setup(hass)
        from homeassistant import loader, config_entries
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        await hass.config_entries.async_initialize()
        loader.async_setup(hass)
        entry = ConfigEntry(version=2, minor_version=1, domain='eon_w1000', title='E.ON W1000', data={}, source='user', entry_id='synthetic', discovery_keys={}, options={}, subentries_data=None, unique_id=None)
        await hass.config_entries.async_add(entry)
        # No IMAP and no Recorder in a unit test: the collectors are stubbed,
        # the service/entity wiring under test is real.
        coordinator_module.EonW1000Coordinator._collect = lambda self, workdir, force: (coordinator_module.RunSelection(), {'mails': 0})

        async def _no_refresh(self):
            return None

        coordinator_module.EonW1000Coordinator.async_config_entry_first_refresh = _no_refresh
        # Platform plumbing is HA's job, not this test's: keep the real
        # async_setup_entry body (ledger restore + service registration) and
        # skip only the entity-platform forwarding.
        async def _no_forward(entry, platforms):
            return None

        hass.config_entries.async_forward_entry_setups = _no_forward
        # A held setup_lock makes HA skip its LOADED-state assertion for the
        # forwarding step, which this test deliberately bypasses.
        await entry.setup_lock.acquire()
        assert await component.async_setup_entry(hass, entry)
        assert hass.services.has_service('eon_w1000', component.SERVICE_IMPORT_FILES)
        assert hass.services.has_service('eon_w1000', component.SERVICE_PROCESS_NOW)
        validated = component.SERVICE_IMPORT_FILES_SCHEMA({'paths': str(tmp_path / 'a.xlsx')})
        assert validated == {'paths': [str(tmp_path / 'a.xlsx')]}, validated
        await hass.async_stop()
    asyncio.run(exercise())

