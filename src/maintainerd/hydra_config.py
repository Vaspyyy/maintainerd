"""Opt-in Hydra configuration. Model policy is explicit, never inferred from prices."""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from .state import Config, Error

STAGES = ('scout', 'explore', 'validate', 'discuss', 'implement', 'review', 'escalate')
EFFORTS = {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'}
ARTIFACT_STAGES = {
    'runs': 'explore', 'threads': 'discuss', 'implementations': 'implement',
    'reviews': 'review', 'scout': 'scout', 'validate': 'validate', 'escalate': 'escalate',
}


@dataclass(frozen=True)
class Profile:
    model: str
    reasoning_effort: str = 'medium'

    @classmethod
    def parse(cls, stage: str, raw: object) -> Profile:
        if not isinstance(raw, dict) or set(raw) - {'model', 'reasoning_effort'}:
            raise Error(f'profiles.{stage} accepts only model and reasoning_effort.')
        model = raw.get('model')
        effort = raw.get('reasoning_effort', 'medium')
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}', model):
            raise Error(f'profiles.{stage}.model must be an explicit model ID.')
        if not isinstance(effort, str) or effort not in EFFORTS:
            raise Error(f'profiles.{stage}.reasoning_effort is not a supported effort spelling.')
        return cls(model, effort)


@dataclass(frozen=True)
class Settings:
    scouts: int = 6
    max_parallel: int = 6
    reserved_core_slots: int = 3
    scout_interval_seconds: int = 120
    poll_seconds: int = 30
    queue_limit: int = 30
    max_validation_attempts: int = 3
    max_escalations_per_day: int = 2
    core_explore: bool = False
    core_explore_seconds: int = 3600
    rate_limit_cooldown_seconds: int = 120
    max_transient_retries: int = 2
    profiles: dict[str, Profile] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Settings:
        try:
            data = tomllib.loads(path.read_text(encoding='utf-8'))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise Error(f'Cannot read {path}. Run maintainerd hydra init first.') from exc
        if set(data) - {'hydra', 'profiles'}:
            raise Error('hydra.toml accepts only [hydra] and [profiles.<stage>] tables.')
        options, profiles = data.get('hydra', {}), data.get('profiles', {})
        if not isinstance(options, dict) or not isinstance(profiles, dict):
            raise Error('Hydra options and profiles must be TOML tables.')
        allowed = {f.name for f in fields(cls)} - {'profiles'}
        if options.keys() - allowed or profiles.keys() - set(STAGES):
            raise Error('Unknown Hydra setting or execution profile.')
        parsed = {stage: Profile.parse(stage, raw) for stage, raw in profiles.items()}
        required = set(STAGES) - {'escalate'}
        if required - parsed.keys():
            raise Error('Missing Hydra profiles: ' + ', '.join(sorted(required - parsed.keys())))
        result = cls(**options, profiles=parsed)
        limits = {
            'scouts': (0, 128), 'max_parallel': (1, 128), 'reserved_core_slots': (1, 128),
            'scout_interval_seconds': (15, 604800), 'poll_seconds': (15, 3600),
            'queue_limit': (1, 10000), 'max_validation_attempts': (1, 10),
            'max_escalations_per_day': (0, 100), 'core_explore_seconds': (60, 604800),
            'rate_limit_cooldown_seconds': (60, 3600), 'max_transient_retries': (0, 5),
        }
        for key, (low, high) in limits.items():
            value = getattr(result, key)
            if type(value) is not int or not low <= value <= high:
                raise Error(f'hydra.{key} must be an integer from {low} to {high}.')
        if type(result.core_explore) is not bool:
            raise Error('hydra.core_explore must be a boolean.')
        if result.reserved_core_slots > result.max_parallel:
            raise Error('reserved_core_slots cannot exceed max_parallel.')
        if result.scouts and result.reserved_core_slots == result.max_parallel:
            raise Error('Scouts need at least one non-reserved execution slot.')
        return result


@dataclass(frozen=True)
class ExecutionConfig(Config):
    """A dataclass so the existing per-maintainer App overrides still work."""
    hydra_home: Path | None = None
    hydra_settings: Settings | None = field(default=None, repr=False)
    hydra_actor: str = ''
    hydra_role: str = 'core'
    reasoning_effort: str | None = None
    execution_stage: str = ''
    inherited_fds: tuple[int, ...] = field(default=(), repr=False)


def execution_config(base: Config, home: Path, actor: str, role: str, settings: Settings) -> ExecutionConfig:
    values = {f.name: getattr(base, f.name) for f in fields(Config)}
    if role == 'scout':
        # A private scout has no App identity or publication configuration.
        values.update(include_github=False, publish_proposals=False,
                      github_app_id=None, github_private_key_path=None)
    return ExecutionConfig(**values, hydra_home=home, hydra_settings=settings,
                           hydra_actor=actor, hydra_role=role)


def resolve(config: Config, artifacts: Path, stage: str | None = None) -> Config:
    if not isinstance(config, ExecutionConfig):
        return config
    selected = stage or config.execution_stage or ARTIFACT_STAGES.get(artifacts.parent.name)
    if selected not in STAGES:
        raise Error('Hydra could not resolve the execution stage; refusing an implicit model fallback.')
    if config.hydra_role == 'scout' and selected != 'scout':
        raise Error('Private scouts may only execute the scout profile.')
    profile = config.hydra_settings.profiles.get(selected) if config.hydra_settings else None
    if profile is None:
        raise Error(f'No explicit {selected} profile is configured. No fallback was used.')
    return replace(config, model=profile.model, reasoning_effort=profile.reasoning_effort,
                   execution_stage=selected)


def template(scout_model: str, core_model: str, escalation_model: str | None) -> str:
    models = {s: (scout_model if s in ('scout', 'explore', 'discuss') else core_model)
              for s in STAGES if s != 'escalate'}
    if escalation_model:
        models['escalate'] = escalation_model
    lines = [
        '# Hydra v1. This file does not contain credentials.',
        '# CLI/subscription access to these model IDs must be verified locally.',
        '# API token-price ratios do not establish included subscription usage ratios.',
        '[hydra]', 'scouts = 6', 'max_parallel = 6', 'reserved_core_slots = 3',
        'scout_interval_seconds = 120', 'poll_seconds = 30', 'queue_limit = 30',
        'max_validation_attempts = 3', 'max_escalations_per_day = 2',
        'core_explore = false', 'core_explore_seconds = 3600',
        'rate_limit_cooldown_seconds = 120', 'max_transient_retries = 2',
    ]
    for stage, model in models.items():
        Profile.parse(stage, {'model': model})
        lines.extend(['', f'[profiles.{stage}]', f'model = {json.dumps(model)}',
                      'reasoning_effort = "medium"'])
    return '\n'.join(lines) + '\n'
