# Clickable Smart Life controls for YK-ZIGBEE

Conti 0.3.15 adds optional dashboard commands from a user-imported Tuya data
model. This is separate from detecting presses on the physical remote.
There is no automatic product/DP guess, cloud runtime dependency, extra
gateway connection or change to the existing discovered sensor mapping.

## Enable the supplied model

1. Update to **v0.3.15** through HACS and restart Home Assistant normally.
2. Open **Settings > Devices & services > Conti**, then the settings gear for
   the **yk** entry (not the Quad/WG-Z1 entry).
3. Keep the existing DP Mapping JSON. In the new **Smart Life control model
   (JSON, optional)** field, paste the complete contents of
   [yk-zigbee-control-model.json](examples/yk-zigbee-control-model.json).
   This file contains the command declarations supplied for model `fjjr8o`;
   it contains no gateway key or account credentials.
4. Submit. The normal options reload creates the controls on the yk device.
5. Use **Add to dashboard** on the device page, or create Button cards for the
   new `button` entities. Button cards can call `button.press` on tap.

The model import also accepts the full Query Things Data Model API response
containing `result.model`. Import only the model belonging to this device.
Invalid declarations are rejected. Clearing the import disables the optional
controls on reload; old inactive registry entries can be deleted in HA.

## Controls and their actual command meanings

| HA control | Declared command | Meaning |
| --- | --- | --- |
| Power ON / Power OFF | DP6 true / false | Request on/off control |
| White / Colour / Scene mode | DP7 white / colour / scene | Request lighting mode |
| Select S1 through Select S8 | DP52 switch_1 through switch_8 | Select the active key/group as in the Smart Life panel |
| Brightness level | DP8 integer 10-1000 | Set an absolute level in model units |
| Colour temperature level | DP9 integer 0-1000 | Set an absolute level in model units, not Kelvin |
| Button mode 1-8 (Configuration) | DP21-28 declared enum values | Configure each key mode; changes remote settings |

This produces **13 virtual buttons, 8 configuration selectors and 2 numeric
controls**. The button modes offer `no_initialize`, `remote_control` and
`wireless_switch`. Adding the controls does not change any mode automatically.

**Select S1-S8 does not simulate physically pressing S1-S8 or run a bound scene.**
It sends the panel's key-selection setting. The Power/Mode/Level functions are
declared writable by the supplied model, but their effect through this gateway's
LAN firmware has not yet been physically verified. Their presence does not
establish support for an arbitrary bound light or for HA physical-button events.

Write-only numeric controls begin unknown. After a successful transport send
they display the last requested value, with `command_only: true`; that is not
a confirmed lamp state. Numeric bounds, scale and step come from the imported
model. Commands fail visibly when the child is offline or the manager cannot
send. A successful send is not proof the gateway executed the requested effect.

The model has no separate verified moon/night command. Raw colour and scene
strings (DP10/DP30) are not exposed because their encoding is unspecified in the
supplied model. No guessed moon, RGB payload, relative brightness command or
physical-press mapping has been added. Colour mode is available via DP7.

## Development validation

Tests cover import/canonical storage without credentials, rejection of malformed
models, exclusion of read-only/raw properties, opt-in isolation from direct
devices, entity names/IDs, repeated presses, enum/numeric validation, offline
and failed sends, command-only values, and routing through the existing manager
to the correct child on one shared parent. No live HA configuration or device
commands were changed during development.

Validation results: **134 focused tests pass**. The full suite reports **346
passed and the same six pre-existing failures**, with 41% overall coverage.
Full Ruff remains at 172 findings and mypy at 64 errors in 19 files, unchanged
from 0.3.14. The four new integration modules pass targeted Ruff and mypy checks.
