"""Bambu material-enclosure sensor history and alarm interpretation."""

import logging

from .ams_drying import is_drying_active, temperature_alarm_suppressed
from .ams_humidity import ams_humidity_percent


def _ams_has_filament(ams_data: dict) -> bool:
    """True if this AMS unit has at least one tray slot holding filament.

    Bambu firmware reports loaded slots via `tray_exist_bits`, a per-AMS hex
    bitmap (one bit per tray slot — bit set = spool present). Empty AMS units
    still report sensor readings, but those readings are ambient and not
    actionable: no filament to dry, no humidity to push down. #1619 — gate
    humidity/temperature alarms on this check so empty units don't generate
    hourly noise. Sensor history still records regardless so the UI charts
    stay continuous.

    Fallback path inspects the `tray` array's `tray_type` fields for setups
    where `tray_exist_bits` is missing (some early-connection pushall shapes).
    """
    bits = ams_data.get("tray_exist_bits")
    if isinstance(bits, str) and bits.strip():
        try:
            return int(bits, 16) > 0
        except ValueError:
            pass
    trays = ams_data.get("tray")
    if isinstance(trays, list):
        return any(
            isinstance(t, dict) and isinstance(t.get("tray_type"), str) and t["tray_type"].strip() for t in trays
        )
    return False


async def record_ams_history():
    """Background task to record AMS humidity and temperature data."""
    from backend.app import main as host

    logger = logging.getLogger(__name__)

    # Wait a short time for MQTT connections to establish on startup
    await host.asyncio.sleep(10)

    while True:
        try:
            from backend.app.models.ams_history import AMSSensorHistory
            from backend.app.models.printer import Printer
            from backend.app.models.settings import Settings

            async with host.async_session() as db:
                # Get all active printers
                result = await db.execute(
                    host.select(Printer).where(Printer.is_active.is_(True), Printer.plugin_id == "bambu")
                )
                printers = result.scalars().all()

                # Get alarm thresholds from settings
                humidity_threshold = 60.0  # Default: fair threshold
                temp_fair_threshold = 35.0  # Display band default (ams_temp_fair)
                result = await db.execute(host.select(Settings).where(Settings.key == "ams_humidity_fair"))
                setting = result.scalar_one_or_none()
                if setting:
                    try:
                        humidity_threshold = float(setting.value)
                    except (ValueError, TypeError):
                        pass  # Keep default threshold if stored value is invalid
                result = await db.execute(host.select(Settings).where(Settings.key == "ams_temp_fair"))
                setting = result.scalar_one_or_none()
                if setting:
                    try:
                        temp_fair_threshold = float(setting.value)
                    except (ValueError, TypeError):
                        pass  # Keep default threshold if stored value is invalid

                # The alarm gets its own threshold, seeded from the resolved fair
                # value so an install that has never set one behaves exactly as
                # it did before (#2905). ams_temp_fair decides when the card turns
                # amber; 35 C is a reasonable place to change a colour and not a
                # reasonable place to page someone. A room above it makes the
                # alarm fire once an hour for as long as the weather lasts, and
                # the only way to stop it was to raise the display band and lose
                # the colour that says the unit is warm.
                #
                # An unset value is stored as the literal "None", which the except
                # below swallows the same way it swallows garbage -- so the
                # fallback costs nothing and needs no sentinel of its own.
                result = await db.execute(host.select(Settings).where(Settings.key == "ams_temp_alarm"))
                setting = result.scalar_one_or_none()
                temp_alarm_threshold = host._resolve_temp_alarm_threshold(
                    temp_fair_threshold, setting.value if setting else None
                )

                # Per-filament humidity threshold overrides (#1605) — resolved
                # per-AMS below from the loaded tray types. Reuses the same
                # resolver as the auto-drying scheduler so behavior stays in
                # lockstep across both consumers.
                from backend.app.services.print_scheduler import PrintScheduler

                per_type_humidity_thresholds: dict[str, int] = {}
                result = await db.execute(host.select(Settings).where(Settings.key == "ams_humidity_thresholds"))
                setting = result.scalar_one_or_none()
                if setting and setting.value:
                    try:
                        raw = host.json.loads(setting.value)
                        if isinstance(raw, dict):
                            for k, v in raw.items():
                                try:
                                    per_type_humidity_thresholds[str(k).upper() if k != "default" else "default"] = int(
                                        v
                                    )
                                except (TypeError, ValueError):
                                    continue
                    except (ValueError, TypeError):
                        pass  # Invalid JSON → no overrides, fall through to global threshold

                # Per-AMS drying latch (#1802), loaded once per pass and written
                # back below only if a unit changed it.
                drying_latch = await host._load_ams_drying_latch(db)
                drying_latch_before = dict(drying_latch)

                recorded_count = 0
                for printer in printers:
                    # Get current state from printer manager
                    state = host.printer_manager.get_status(printer.id)
                    if not state or not state.connected or not state.raw_data:
                        continue  # Skip disconnected printers - don't use stale data

                    raw_data = state.raw_data
                    if "ams" not in raw_data or not isinstance(raw_data["ams"], list):
                        continue

                    # Record data for each AMS unit
                    for ams_data in raw_data["ams"]:
                        ams_id = int(ams_data.get("id", 0))

                        # Percentage only. The 1-5 index is inverted, so
                        # charting it as a percentage drew the wettest units as
                        # the driest (#3140); a unit that reports no percentage
                        # leaves a gap in the chart instead. See
                        # utils/ams_humidity.
                        humidity = ams_humidity_percent(ams_data)

                        # No supported printer is known to send the index
                        # alone -- the report came from unsupported firmware,
                        # and no install has been seen using the old fallback.
                        # "Known" is doing work there, so say so once per unit:
                        # the alternative is a silent blank card.
                        if humidity is None and ams_data.get("humidity") is not None:
                            unit_key = (printer.id, ams_id)
                            if unit_key not in host._ams_index_only_logged:
                                host._ams_index_only_logged.add(unit_key)
                                logger.info(
                                    "[%s] AMS %d reports the 1-5 humidity index but no usable humidity_raw "
                                    "percentage. The index is inverted and is not shown as a percentage "
                                    "(#3140), so this unit has no humidity reading, chart or alarm. "
                                    "Please report this with the printer and AMS firmware versions.",
                                    printer.name,
                                    ams_id,
                                )

                        # Get temperature
                        temperature = None
                        temp_str = ams_data.get("temp")
                        if temp_str is not None:
                            try:
                                temperature = float(temp_str)
                            except (ValueError, TypeError):
                                pass  # Skip unparseable temperature value

                        # Skip if no data
                        if humidity is None and temperature is None:
                            continue

                        # Record the data point
                        history = AMSSensorHistory(
                            printer_id=printer.id,
                            ams_id=ams_id,
                            humidity=humidity,
                            # Both columns hold the same reading now that the
                            # index can no longer reach ``humidity``. Writing it
                            # through the same value also stops a genuine 0%
                            # from being stored as NULL, which the old truthiness
                            # test did.
                            humidity_raw=humidity,
                            temperature=temperature,
                        )
                        db.add(history)
                        recorded_count += 1

                        # Generate AMS label and determine if it's AMS-HT (A, B, C, D or HT-A for AMS-Lite/Hub)
                        is_ams_ht = ams_id >= 128
                        if is_ams_ht:
                            ams_label = f"HT-{chr(65 + (ams_id - 128))}"
                        else:
                            ams_label = f"AMS-{chr(65 + ams_id)}"

                        # Skip alarm dispatch for empty AMS units — humidity /
                        # temperature readings are ambient with no filament to
                        # protect, and the hourly notification just becomes
                        # noise. Sensor history was already recorded above so
                        # the UI charts stay continuous (#1619). Per-AMS check
                        # so a multi-AMS setup with one loaded + one empty
                        # still alarms on the loaded unit.
                        if not _ams_has_filament(ams_data):
                            continue

                        # Resolve per-filament humidity threshold for this AMS
                        # unit (#1605). Falls back to the global ams_humidity_fair
                        # when no per-type overrides are configured.
                        trays = ams_data.get("tray", []) or []
                        effective_humidity_threshold = float(
                            PrintScheduler.resolve_humidity_threshold(
                                trays, per_type_humidity_thresholds, int(humidity_threshold)
                            )
                        )

                        # Check humidity alarm (only if above threshold)
                        if humidity is not None and humidity > effective_humidity_threshold:
                            cooldown_key = f"{printer.id}:{ams_id}:humidity"
                            last_alarm = host._ams_alarm_cooldown.get(cooldown_key)
                            now = host.datetime.now(host.timezone.utc)
                            if (
                                last_alarm is None
                                or (now - last_alarm).total_seconds() >= host.AMS_ALARM_COOLDOWN_MINUTES * 60
                            ):
                                host._ams_alarm_cooldown[cooldown_key] = now
                                logger.info(
                                    f"Sending humidity alarm for {printer.name} {ams_label}: {humidity}% > {effective_humidity_threshold}%"
                                )
                                try:
                                    # Call different notification method based on AMS type
                                    if is_ams_ht:
                                        await host.notification_service.on_ams_ht_humidity_high(
                                            printer.id,
                                            printer.name,
                                            ams_label,
                                            humidity,
                                            effective_humidity_threshold,
                                            db,
                                        )
                                    else:
                                        await host.notification_service.on_ams_humidity_high(
                                            printer.id,
                                            printer.name,
                                            ams_label,
                                            humidity,
                                            effective_humidity_threshold,
                                            db,
                                        )
                                except Exception as e:
                                    logger.warning("Failed to send humidity alarm: %s", e)

                        # A drying cycle heats the unit far past ams_temp_fair on
                        # purpose — 45 C for PLA, 65 C for PETG, 85 C on an
                        # AMS-HT, against a 35 C default — so the alarm fired
                        # once an hour for the whole cycle and kept firing while
                        # the unit cooled back down (#1802). Latch on the
                        # firmware's own drying state and hold until the reading
                        # returns to normal. Humidity is deliberately left alone:
                        # it falls during drying, which is the whole point.
                        latch_key = f"{printer.id}:{ams_id}"
                        # The latch releases at `threshold`, so it takes the alarm
                        # number too. Handing it the display band would strand the
                        # latch on any unit that settles back above it -- a room
                        # where the AMS rests at 37.7 C never returns under a 35 C
                        # band, so the latch could only expire on the grace cap
                        # rather than releasing when the unit had actually cooled.
                        suppress_temp_alarm, new_latch = temperature_alarm_suppressed(
                            drying_active=is_drying_active(ams_data),
                            temperature=temperature,
                            threshold=temp_alarm_threshold,
                            latched_at=drying_latch.get(latch_key),
                            now=host.datetime.now(host.timezone.utc),
                            grace_minutes=host.AMS_DRYING_GRACE_MINUTES,
                        )
                        if new_latch is None:
                            drying_latch.pop(latch_key, None)
                        else:
                            drying_latch[latch_key] = new_latch

                        # Check temperature alarm (only if above threshold)
                        if temperature is not None and temperature > temp_alarm_threshold and not suppress_temp_alarm:
                            cooldown_key = f"{printer.id}:{ams_id}:temperature"
                            last_alarm = host._ams_alarm_cooldown.get(cooldown_key)
                            now = host.datetime.now(host.timezone.utc)
                            if (
                                last_alarm is None
                                or (now - last_alarm).total_seconds() >= host.AMS_ALARM_COOLDOWN_MINUTES * 60
                            ):
                                host._ams_alarm_cooldown[cooldown_key] = now
                                logger.info(
                                    f"Sending temperature alarm for {printer.name} {ams_label}: "
                                    f"{temperature}°C > {temp_alarm_threshold}°C"
                                )
                                try:
                                    # Call different notification method based on AMS type
                                    if is_ams_ht:
                                        # The reported threshold has to be the one
                                        # that fired, or the message says "> 35 °C"
                                        # while firing at 45.
                                        await host.notification_service.on_ams_ht_temperature_high(
                                            printer.id, printer.name, ams_label, temperature, temp_alarm_threshold, db
                                        )
                                    else:
                                        await host.notification_service.on_ams_temperature_high(
                                            printer.id, printer.name, ams_label, temperature, temp_alarm_threshold, db
                                        )
                                except Exception as e:
                                    logger.warning("Failed to send temperature alarm: %s", e)

                if drying_latch != drying_latch_before:
                    await host._save_ams_drying_latch(db, drying_latch)

                await db.commit()
                if recorded_count > 0:
                    logger.info("Recorded %s AMS sensor history entries", recorded_count)

                # Periodic cleanup of old data (every ~288 recordings = ~24 hours at 5min interval)

                host._ams_cleanup_counter += 1
                if host._ams_cleanup_counter >= 288:
                    host._ams_cleanup_counter = 0
                    # Get retention days from settings
                    from backend.app.models.settings import Settings

                    result = await db.execute(host.select(Settings).where(Settings.key == "ams_history_retention_days"))
                    setting = result.scalar_one_or_none()
                    retention_days = host._retention_days(
                        setting.value if setting else None, host.AMS_HISTORY_RETENTION_DAYS
                    )

                    cutoff = host.utcnow_naive() - host.timedelta(days=retention_days)
                    result = await db.execute(
                        host.delete(AMSSensorHistory).where(AMSSensorHistory.recorded_at < cutoff)
                    )
                    await db.commit()
                    if result.rowcount > 0:
                        logger.info(
                            f"Cleaned up {result.rowcount} old AMS sensor history entries (older than {retention_days} days)"
                        )

            # Wait until next recording interval
            await host.asyncio.sleep(host.AMS_HISTORY_INTERVAL)

        except host.asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("AMS history recording failed: %s", e)
            await host.asyncio.sleep(60)  # Wait a bit before retrying
