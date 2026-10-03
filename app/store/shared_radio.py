"""This radio's identity, keys and paired contacts, shared with every MFruit
OS radio app (the Messenger, ...) through the SDK's shared radio store.

MFruit OS keeps them once per device (``~/.whisplay-os/shared/radio``), so a
radio paired here is paired in the Messenger too, and the other way round.
On first use our own files are handed over -- the Device ID in settings.json
and keys.json -- and never changed or removed; radios already paired with
this one keep recognising it.

Without the SDK's radio module (an older SDK copy, no ``cryptography``) the
app keeps using its own files, exactly as before.
"""

from __future__ import annotations

from app.utils.logger import get_logger

log = get_logger("shared-radio")


def _sdk():
    try:
        from mfruit_sdk.radio import contacts, legacy, settings
        from mfruit_sdk.radio.keyring import Keyring
    except Exception as exc:          # older SDK copy, or no cryptography
        log.info("shared radio store unavailable (%s); using this app's own files", exc)
        return None
    return settings, legacy, contacts, Keyring


def sync_identity(settings, overrides) -> bool:
    """Make ``settings`` use the shared Device ID, name and radio settings.

    The shared identity is seeded from ours if it has none yet, so an
    existing WalkieTalkie keeps its ID. True if the shared store is in use.
    """
    sdk = _sdk()
    if sdk is None:
        return False
    shared, legacy, _contacts, _keyring = sdk
    try:
        legacy.adopt_walkietalkie()
        device = shared.load_device(legacy_address=settings.radio.address,
                                    legacy_name=settings.identity.callsign)
    except Exception:
        log.warning("cannot use the shared radio identity", exc_info=True)
        return False
    if device.address != settings.radio.address:
        log.warning("Device ID %s -> %d: the ID this device's radio apps share",
                    settings.radio.address, device.address)
        overrides.set("radio", "address", device.address)
        settings.radio.address = device.address
    # The settings MFruit OS's radio setup wrote into the module are the ones
    # it actually holds: pace packets and count airtime by them.
    provisioned = shared.load_radio()
    if provisioned is not None:
        settings.radio.frequency_mhz = provisioned.frequency_mhz
        settings.radio.air_speed = provisioned.air_speed
        settings.radio.port = provisioned.port or settings.radio.port
    return True


def open_keyring(data_dir):
    """The shared keyring (adopting our keys.json once), or our own as before."""
    sdk = _sdk()
    if sdk is not None:
        try:
            return sdk[3](legacy_path=str(data_dir / "keys.json"))
        except Exception:
            log.warning("cannot open the shared keys; using this app's own", exc_info=True)
    from app.store.keyring import Keyring
    return Keyring(data_dir)


def shared_contacts():
    """{address: name} of radios paired in any radio app, or {}."""
    sdk = _sdk()
    if sdk is None:
        return {}
    try:
        return sdk[2].Contacts().all()
    except Exception:
        log.warning("cannot read the shared contacts", exc_info=True)
        return {}


def remember_contact(address: int, name: str):
    sdk = _sdk()
    if sdk is None or not name:
        return
    try:
        sdk[2].Contacts().set(address, name)
    except Exception:
        log.warning("cannot save %s to the shared contacts", name, exc_info=True)


def forget_contact(address: int):
    sdk = _sdk()
    if sdk is None:
        return
    try:
        sdk[2].Contacts().remove(address)
    except Exception:
        log.warning("cannot remove %d from the shared contacts", address, exc_info=True)


def save_identity(address: int, name: str):
    """Share a Device ID or name changed here, so the Messenger uses it too."""
    sdk = _sdk()
    if sdk is None:
        return
    try:
        sdk[0].save_device(sdk[0].Device(address, name))
    except Exception:
        log.warning("cannot share Device ID %d", address, exc_info=True)
