"""Offline integration test against real HA Recorder and isolated SQLite."""
import asyncio
from datetime import datetime, timedelta
from functools import partial

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period, list_statistic_ids
from homeassistant.helpers import frame
from homeassistant.util import dt as dt_util

from test_core import write_export, TZ
from custom_components.eon_w1000.coordinator import EonW1000Coordinator
from custom_components.eon_w1000.const import STATISTIC_IMPORT_ID, STATISTIC_EXPORT_ID


def test_real_recorder_source_import_replay_metadata_and_changes(tmp_path):
    async def exercise():
        hass = HomeAssistant(str(tmp_path))
        frame.async_setup(hass)
        from homeassistant import loader, config_entries
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        await hass.config_entries.async_initialize()
        loader.async_setup(hass)
        from homeassistant.helpers.recorder import async_initialize_recorder
        async_initialize_recorder(hass)
        dt_util.set_default_time_zone(TZ)
        assert await async_setup_component(hass, 'recorder', {'recorder': {'db_url': f'sqlite:///{tmp_path}/test.db', 'auto_purge': False}})
        await hass.async_start()
        recorder = get_instance(hass)
        await recorder.async_block_till_done()
        coordinator = EonW1000Coordinator(hass, {})
        path = write_export(tmp_path / 'source.xlsx', days=2)
        try:
            result = await coordinator.async_import_files([str(path)])
            assert result['status'] == 'ok'
            await recorder.async_block_till_done()
            start = datetime(2026, 9, 23, tzinfo=TZ)
            end = start + timedelta(days=2)
            async def read():
                return await recorder.async_add_executor_job(partial(statistics_during_period, hass, start, end, {STATISTIC_IMPORT_ID, STATISTIC_EXPORT_ID}, 'hour', None, {'sum', 'state', 'change'}))
            first = await read()
            for identity, channel in [(STATISTIC_IMPORT_ID, 'ap'), (STATISTIC_EXPORT_ID, 'am')]:
                assert len(first[identity]) == 48
                expected = sorted(coordinator._source_hours.values(), key=lambda h: h.start)
                assert [round(r['change'], 3) for r in first[identity]] == [round(getattr(h, channel), 3) for h in expected]
            await coordinator.async_import_files([str(path)])
            await recorder.async_block_till_done()
            assert await read() == first
            metadata = await recorder.async_add_executor_job(partial(list_statistic_ids, hass, {STATISTIC_IMPORT_ID, STATISTIC_EXPORT_ID}, None))
            assert len(metadata) == 2
            assert all(m['has_sum'] and m['statistics_unit_of_measurement'] == 'kWh' and m['source'] == 'recorder' for m in metadata)
            legacy = await recorder.async_add_executor_job(partial(list_statistic_ids, hass, {'sensor.grid_energy_import', 'sensor.grid_energy_export'}, None))
            assert not legacy
        finally:
            await hass.async_stop()
    asyncio.run(exercise())
