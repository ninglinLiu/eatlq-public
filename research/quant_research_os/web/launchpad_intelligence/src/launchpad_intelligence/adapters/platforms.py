from __future__ import annotations

from .evm import EVMAdapter


class NoxaAdapter(EVMAdapter):
    launchpad_id = "noxa"
    chain = "robinhood"


class PonsAdapter(EVMAdapter):
    launchpad_id = "pons"
    chain = "robinhood"


class FlapAdapter(EVMAdapter):
    launchpad_id = "flap_robinhood"
    chain = "robinhood"


class ClankerAdapter(EVMAdapter):
    launchpad_id = "clanker"
    chain = "base"


class DopplerAdapter(EVMAdapter):
    launchpad_id = "doppler"
    chain = "base"


class FlaunchAdapter(EVMAdapter):
    launchpad_id = "flaunch"
    chain = "base"


ADAPTERS = {
    cls.launchpad_id: cls
    for cls in (NoxaAdapter, PonsAdapter, FlapAdapter, ClankerAdapter, DopplerAdapter, FlaunchAdapter)
}

