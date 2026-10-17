"""Daemon roles and the warm floor.

The execution-mode flag (`always_on` / `on_demand`) is gone. "all" now means the
always-on set — scheduler + controller — and the dispatcher runs only in the
workers the controller spawns. How many of those exist per org is the warm floor.
"""


from lucent import settings as runtime_settings


def test_default_roles_are_the_always_on_set():
    """roles='all' supervises; it does not dispatch in-process."""
    from daemon.daemon import LucentDaemon

    assert LucentDaemon._parse_roles("all") == {"scheduler", "controller"}


def test_dispatcher_is_explicit_only():
    from daemon.daemon import LucentDaemon

    assert LucentDaemon._parse_roles("dispatcher") == {"dispatcher"}
    assert LucentDaemon._parse_roles("all,dispatcher") == {
        "scheduler",
        "controller",
        "dispatcher",
    }


def test_unknown_roles_are_dropped():
    from daemon.daemon import LucentDaemon

    assert LucentDaemon._parse_roles("scheduler,bogus") == {"scheduler"}


def test_resolve_roles_matches_parse():
    from daemon.daemon import LucentDaemon

    daemon = LucentDaemon.__new__(LucentDaemon)
    assert daemon._resolve_roles("all") == {"scheduler", "controller"}


def test_warm_floor_defaults_to_one():
    """One worker stays warm by default, so work never pays a cold start."""
    assert runtime_settings.daemon_worker_min_warm() == 1


def test_warm_floor_setting_is_defined_and_has_no_choice_list():
    definition = runtime_settings.get_runtime_setting_definition("daemon.worker_min_warm")
    assert definition is not None
    assert definition.env_var == "LUCENT_DAEMON_WORKER_MIN_WARM"
    assert definition.default == 1
    assert definition.min_value == 0


def test_the_old_mode_setting_is_gone():
    assert runtime_settings.get_runtime_setting_definition("daemon.mode") is None
