"""Implements "unified" FGD files.

This allows sharing definitions among different engine versions.
"""
from __future__ import annotations
from typing import Any, TypeVar, Callable
from collections.abc import MutableMapping
from collections import Counter, defaultdict, ChainMap
from pathlib import Path
import argparse
import sys

from srctools import fgd
from srctools.fgd import (
    FGD, AutoVisgroup, EntAttribute, EntityDef, EntityTypes, Helper, HelperExtAppliesTo,
    HelperTypes, KVDef, Snippet, ValueTypes, match_tags, validate_tags,
)
from srctools.filesys import File, RawFileSystem
from srctools.math import Vec, format_float


# Chronological order of games.
# If 'since_hl2' etc is used in FGD, all future games also include it.
# If 'until_l4d' etc is used in FGD, only games before include it.
GAMES_CHRONO: list[tuple[str, str]] = [
    ('HL2', 'Half-Life 2'),
    ('EP1', 'Half-Life 2: Episode One'),
    ('EP2', 'Half-Life 2: Episode Two'),

    ('TF2',   'Team Fortress 2'),
    ('P1',    'Portal'),
    ('L4D',   'Left 4 Dead'),
    ('L4D2',  'Left 4 Dead 2'),
    ('ASW',   'Alien Swarm'),
    ('P2',    'Portal 2'),
    ('CSGO',  'Counter-Strike: Global Offensive'),

    ('SFM',   'Source Filmmaker'),
    ('DOTA2', 'Dota 2'),
]

# Additional mods/games, which branched off of mainline ones.
MODS_BRANCHED: dict[str, list[tuple[str, str]]] = {
    'HL2': [
        ('HLS', 'Half-Life: Source'),
        ('DODS', 'Day of Defeat: Source'),
        ('CSS',  'Counter-Strike: Source'),
    ],
    'EP2': [
        ('MESA', 'Black Mesa'),
        ('GMOD', "Garry's Mod"),
        ('EZ1', 'Entropy: Zero'),
        ('EZ2', 'Entropy: Zero 2'),
        ('KZ', 'Kreedz Climbing'),
    ],
    'P1': [
        ('PEE15', 'Portal Epic Edition 1.5'),
    ],
    'ASW': [
        ('ASRD', 'Alien Swarm: Reactive Drop'),
    ],
    'P2': [
        ('P2SIXENSE', 'Portal 2 Sixense MotionPack'),
        ('P2EDU', 'Portal 2 Educational Version'),
        ('STANLEY', 'The Stanley Parable'),
        ('INFRA', 'INFRA'),
        ('PEE2', 'Portal Epic Edition 2'),
    ],
    'CSGO': [
        ('P2DES', 'Portal 2: Desolation'),
    ],
}
MOD_TO_BRANCH = {
    mod: branch
    for branch, mods in MODS_BRANCHED.items()
    for mod, _ in mods
}
ALL_MODS = {
    *MOD_TO_BRANCH,
    'MBASE',  # Mapbase can either be episodic or hl2 base, specify it with those.
}
GAME_ORDER = [game for game, _ in GAMES_CHRONO]
ALL_GAMES = set(GAME_ORDER)

# Specific features that are backported to various games.

FEATURES: dict[str, set[str]] = {
    'EP1': {'HL2'},
    'EP2': {'HL2', 'EP1'},

    'MBASE': {'VSCRIPT'},
    'MESA': {'HL2', 'INST_IO'},
    'GMOD': {'HL2', 'EP1', 'EP2'},
    'EZ1': {'HL2', 'EP1', 'EP2', 'MBASE', 'VSCRIPT'},
    'EZ2': {'HL2', 'EP1', 'EP2', 'MBASE', 'VSCRIPT'},
    'KZ': {'HL2'},

    'L4D2': {'INST_IO', 'VSCRIPT'},
    'TF2': {'PROP_SCALING', 'VSCRIPT'},
    'ASW': {'INST_IO', 'VSCRIPT'},
    'P2': {'INST_IO', 'VSCRIPT'},
    'CSGO': {'INST_IO', 'PROP_SCALING', 'VSCRIPT', 'PROPCOMBINE'},
    'P2DES': {'P2', 'INST_IO', 'PROP_SCALING', 'VSCRIPT', 'PROPCOMBINE'},

    'PEE15': {'P1', 'HL2', 'EP1', 'EP2', 'MBASE', 'VSCRIPT'},
    'PEE2': {'P2', 'HL2', 'EP1', 'EP2', 'INST_IO', 'VSCRIPT'},
}

ALL_FEATURES = {
    tag.upper()
    for t in FEATURES.values()
    for tag in t
}

# Specially handled tags.
TAGS_SPECIAL = {
  'ENGINE',  # Tagged on entries that specify machine-oriented types and defaults.
  'SRCTOOLS',  # Implemented by the srctools post-compiler.
  'PROPPER',  # Propper's added pseudo-entities.
  'COMPLETE',  # KVs that exist, but aren't often or usually used.
  'BEE2',  # BEEmod's templates.
}

TAGS_EMPTY: frozenset[str] = frozenset()
TAGS_NOT_ENGINE: frozenset[str] = frozenset({'-ENGINE', '!ENGINE'})

ALL_TAGS = {
    *ALL_GAMES, *ALL_MODS, *ALL_FEATURES, *TAGS_SPECIAL,
    *{
        prefix + t.upper()
        for prefix in ['SINCE_', 'UNTIL_']
        for t in GAME_ORDER
    },
}

# If the tag is present, run to backport newer FGD syntax to older engines.
POLYFILLS: list[tuple[frozenset[str], Callable[[FGD], None]]] = []
PolyfillFuncT = TypeVar('PolyfillFuncT', bound=Callable[[FGD], None])

# This ends up being the C1 Reverse Line Feed in CP1252,
# which Hammer displays as nothing. We can suffix visgroups with this to
# have duplicates with the same name.
VISGROUP_SUFFIX = '\x8D'

# Special classname which has all the keyvalues and IO of CBaseEntity.
BASE_ENTITY = '_CBaseEntity_'

MAP_SIZE_DEFAULT = 16384  # Default grid bounds.


# Helpers which are only used by one or two entities each.
UNIQUE_HELPERS = {
    fgd.HelperBreakableSurf, fgd.HelperDecal,
    fgd.HelperEnvSprite, fgd.HelperInstance, fgd.HelperLight, fgd.HelperLightSpot,
    fgd.HelperModelLight, fgd.HelperOverlay, fgd.HelperOverlayTransition, fgd.HelperWorldText,
}

# Attribute, display name.
SNIPPET_KINDS = [
    ('snippet_choices', 'choices'),
    ('snippet_desc', 'description'),
    ('snippet_flags', 'spawnflags'),
    ('snippet_input', 'input'),
    ('snippet_keyvalue', 'keyvalue'),
    ('snippet_output', 'output'),
]

# Set of entity classnames which have snippets in their file. If they do, assume all KVs were deduplicated.
SNIPPET_USED: set[str] = set()


def _polyfill(*tags: str) -> Callable[[PolyfillFuncT], PolyfillFuncT]:
    """Register a polyfill, which backports newer FGD syntax to older engines."""
    def deco(func: PolyfillFuncT) -> PolyfillFuncT:
        """Registers the function."""
        POLYFILLS.append((frozenset(tag.upper() for tag in tags), func))
        return func
    return deco


@_polyfill('until_asw', 'mesa')
def _polyfill_boolean(fgd: FGD) -> None:
    """Before Alien Swarm's Hammer, boolean was not available as a keyvalue type.

    Substitute with choices.
    """
    for ent in fgd.entities.values():
        for tag_map in ent.keyvalues.values():
            for kv in tag_map.values():
                if kv.type is ValueTypes.BOOL:
                    kv.type = ValueTypes.CHOICES
                    kv.val_list = [
                        ('0', 'No', TAGS_EMPTY),
                        ('1', 'Yes', TAGS_EMPTY)
                    ]


@_polyfill('until_asw')
def _polyfill_particlesystem(fgd: FGD) -> None:
    """Before Alien Swarm's Hammer, the particle system viewer was not available.

    Substitute with just a string.
    """
    for ent in fgd.entities.values():
        for tag_map in ent.keyvalues.values():
            for kv in tag_map.values():
                if kv.type is ValueTypes.STR_PARTICLE:
                    kv.type = ValueTypes.STRING


@_polyfill('until_asw')
def _polyfill_node_id(fgd: FGD) -> None:
    """Before Alien Swarm's Hammer, node_id was not available as a keyvalue type.

    Substitute with integer.
    """
    for ent in fgd.entities.values():
        for tag_map in ent.keyvalues.values():
            for kv in tag_map.values():
                if kv.type is ValueTypes.TARG_NODE_SOURCE:
                    kv.type = ValueTypes.INT


@_polyfill('until_l4d2', '!tf2')
def _polyfill_scripts(fgd: FGD) -> None:
    """Before L4D2's Hammer (except TF2), the vscript specific types were not available.

    Substitute with just a string.
    """
    for ent in fgd.entities.values():
        for tag_map_kv in ent.keyvalues.values():
            for kv in tag_map_kv.values():
                if kv.type is ValueTypes.STR_VSCRIPT or kv.type is ValueTypes.STR_VSCRIPT_SINGLE:
                    kv.type = ValueTypes.STRING
        for tag_map_io in ent.inputs.values():
            for inp in tag_map_io.values():
                if inp.type is ValueTypes.STR_VSCRIPT_SINGLE:
                    inp.type = ValueTypes.STRING


@_polyfill()
def _polyfill_ext_valuetypes(fgd: FGD) -> None:
    # Convert extension types to their real versions.
    decay = {
        ValueTypes.EXT_STR_TEXTURE: ValueTypes.STRING,
        ValueTypes.EXT_ANGLE_PITCH: ValueTypes.FLOAT,
        ValueTypes.EXT_ANGLES_LOCAL: ValueTypes.ANGLES,
        ValueTypes.EXT_VEC_DIRECTION: ValueTypes.VEC,
        ValueTypes.EXT_VEC_LOCAL: ValueTypes.VEC,
    }
    for ent in fgd.entities.values():
        for tag_map in ent.keyvalues.values():
            for kv in tag_map.values():
                kv.type = decay.get(kv.type, kv.type)


@_polyfill('!P2DES')  # Fixed in VitaminSource.
def _polyfill_frustum_literals(fgd: FGD) -> None:
    """The frustum() helper does not support literal values, only keyvalues."""
    keys = [
        ('fov', '_frustum_fov', '<Frustum FOV>'),
        ('near_z', '_frustum_near', '<Frustum Near>'),
        ('far_z', '_frustum_far', '<Frustum Far>'),
        ('color', '_frustum_color', '<Frustum Color>'),
    ]
    for ent in fgd.entities.values():
        for helper in ent.helpers:
            if helper.TYPE is not HelperTypes.FRUSTUM:
                continue
            for attr, name_base, disp_name in keys:
                value: str | int | tuple[int, int, int] = getattr(helper, attr)
                if isinstance(value, str):
                    continue
                # This is a literal, synthesize a keyvalue.
                i = 0
                name = name_base
                while name in ent.keyvalues:
                    i += 1
                    name = f'{name_base}{i}'
                # This should be !ENGINE, but we don't run polyfills in engine mode.
                ent.keyvalues[name] = {frozenset(): KVDef(
                    name,
                    type=ValueTypes.COLOR_255 if attr == 'color' else ValueTypes.FLOAT,
                    disp_name=disp_name,
                    default=str(Vec(value)) if isinstance(value, tuple) else format_float(value),
                    desc='Ignore, this is necessary to display the preview frustum.',
                    readonly=True,
                )}
                setattr(helper, attr, name)


def format_all_tags() -> str:
    """Append a formatted description of all allowed tags to a message."""

    return (
        f'- Games: {", ".join(GAME_ORDER)}\n'
        '- SINCE_<game>\n'
        '- UNTIL_<game>\n'
        f' Mods: {", ".join(sorted(ALL_MODS))}\n'
        f'- Features: {", ".join(ALL_FEATURES)}\n'
        f'- Special: {", ".join(TAGS_SPECIAL)}\n'
     )


def expand_tags(tags: frozenset[str]) -> frozenset[str]:
    """Expand the given tags, producing the full list of tags these will search.

    This adds since_/until_ tags, and values in FEATURES.
    """
    exp_tags = set(tags)

    # Figure out the game branch, for adding since/until tags.
    # For games, pick the most recent one. For mods, pick the associated branch,
    # but don't add the branch itself - they can do that via FEATURES.
    pos = -1
    for tag in tags:
        tag = tag.upper()
        if tag in ALL_GAMES:
            pos = max(pos, GAME_ORDER.index(tag))
            break
        else:
            try:
                pos = GAME_ORDER.index(MOD_TO_BRANCH[tag])
            except (KeyError, ValueError):
                pass
            else:
                break

    if pos != -1:
        exp_tags.update(
            'SINCE_' + tag
            for tag in GAME_ORDER[:pos + 1]
        )
        exp_tags.update(
            'UNTIL_' + tag
            for tag in GAME_ORDER[pos + 1:]
        )

    for tag in list(exp_tags):
        try:
            exp_tags.update(FEATURES[tag.upper()])
        except KeyError:
            pass

    return frozenset(exp_tags)


def ent_path(ent: EntityDef) -> str:
    """Return the path in the database this entity should be found at."""
    # Very special entity, put in root.
    if ent.classname == 'worldspawn':
        return 'worldspawn.fgd'

    if ent.type is EntityTypes.BASE:
        folder = 'bases'
    else:
        if ent.type is EntityTypes.BRUSH:
            folder = 'brush'
        else:
            folder = 'point'

        if '_' in ent.classname:
            folder += '/' + ent.classname.split('_', 1)[0]

    return f'{folder}/{ent.classname}.fgd'


def load_database(
    dbase: Path,
    extra_loc: Path | None = None,
    fgd_vis: bool = False,
    map_size: int = MAP_SIZE_DEFAULT,
) -> tuple[FGD, EntityDef]:
    """Load the entire database from disk. This returns the FGD, plus the CBaseEntity definition."""
    print(f'Loading database {dbase}:')
    fgd = FGD()

    fgd.map_size_min = -map_size
    fgd.map_size_max = map_size

    # Classname -> filename
    ent_source: dict[str, str] = {}

    fsys = RawFileSystem(str(dbase))
    # First, load the snippets files.
    for file in dbase.rglob("snippets/*.fgd"):
        rel_loc = file.relative_to(dbase)
        load_file(fgd, ent_source, fsys, fsys[str(rel_loc)], is_snippet=True, fgd_vis=fgd_vis)
    # Then, everything else.
    for file in dbase.rglob("*.fgd"):
        rel_loc = file.relative_to(dbase)
        if 'snippets' not in rel_loc.parts:
            load_file(fgd, ent_source, fsys, fsys[str(rel_loc)], is_snippet=False, fgd_vis=fgd_vis)

    load_visgroup_conf(fgd, dbase)

    if extra_loc is not None:
        print('\nLoading extra file:')
        if extra_loc.is_file():
            # One file.
            fsys = RawFileSystem(str(extra_loc.parent))
            fgd.parse_file(
                fsys,
                fsys[extra_loc.name],
                eval_bases=False,
            )
        else:
            print('\nLoading extra files:')
            fsys = RawFileSystem(str(extra_loc))
            for file in extra_loc.rglob("*.fgd"):
                fgd.parse_file(
                    fsys,
                    fsys[str(file.relative_to(extra_loc))],
                    eval_bases=False,
                )
                print('.', end='', flush=True)
    print()

    fgd.apply_bases()

    print('\nDone!')

    print('Entities without visgroups:')
    vis_ents = {
        name.casefold()
        for group in fgd.auto_visgroups.values()
        for name in group.ents
    }
    vis_count = ent_count = 0
    for ent in fgd:
        # Base ents, worldspawn, or engine-only ents don't need visgroups.
        if ent.type is EntityTypes.BASE or ent.classname == 'worldspawn':
            continue
        applies_to = get_appliesto(ent)
        if '+ENGINE' in applies_to or 'ENGINE' in applies_to:
            continue
        ent_count += 1
        if ent.classname.casefold() not in vis_ents:
            print(ent.classname, end=', ')
        else:
            vis_count += 1
    print(f'\nVisgroup count: {vis_count}/{ent_count} ({vis_count*100/ent_count:.2f}%) done!')

    try:
        base_entity_def = fgd.entities.pop(BASE_ENTITY.casefold())
        base_entity_def.type = EntityTypes.BASE
    except KeyError:
        base_entity_def = EntityDef(EntityTypes.BASE)
    return fgd, base_entity_def


def load_visgroup_conf(fgd: FGD, dbase: Path) -> None:
    """Parse through the visgroup.cfg file, adding these visgroups."""
    cur_path: list[str] = []
    # Visgroups don't allow duplicating names. Work around that by adding an
    # invisible suffix.
    group_count: dict[str, int] = Counter()
    try:
        f = (dbase / 'visgroups.cfg').open()
    except FileNotFoundError:
        return
    with f:
        for line in f:
            indent = len(line) - len(line.lstrip('\t'))
            line = line.strip()
            if not line or line.startswith(('#', '//')):
                continue
            cur_path = cur_path[:indent]  # Dedent
            bulleted = line[0] in '-*'
            if (bulleted and '`' not in line) or '(' in line or ')' in line:  # Visgroup.
                single_ent: str | None
                try:
                    vis_name, single_ent = line.lstrip('*-').split('(', 1)
                except ValueError:
                    vis_name = line[1:].strip()
                    single_ent = None
                else:
                    vis_name = vis_name.strip()
                    single_ent = single_ent.strip(' \t`)')

                dupe_count = group_count[vis_name.casefold()]
                if dupe_count:
                    vis_name = vis_name + (VISGROUP_SUFFIX * dupe_count)
                group_count[vis_name.casefold()] = dupe_count + 1

                cur_path.append(vis_name)
                try:
                    visgroup = fgd.auto_visgroups[vis_name.casefold()]
                except KeyError:
                    if indent == 0:  # Don't add Auto itself.
                        continue
                    visgroup = fgd.auto_visgroups[vis_name.casefold()] = AutoVisgroup(vis_name, cur_path[-2])
                if single_ent is not None:
                    visgroup.ents.add(single_ent.casefold())

            elif bulleted:  # Entity.
                ent_name = line[1:].strip('\t `')
                for vis_parent, vis_name in zip(cur_path, cur_path[1:]):
                    visgroup = fgd.auto_visgroups[vis_name.casefold()]
                    visgroup.ents.add(ent_name)


def load_file(
    base_fgd: FGD,
    ent_source: dict[str, str],
    fsys: RawFileSystem,
    file: File,
    *,
    is_snippet: bool,
    fgd_vis: bool,
) -> None:
    """Load an addititional file into the database.

    This is done in a separate FGD first, so we can check for overlapping definitions.
    """
    file_fgd = FGD()
    path = file.path

    # For snippet files, we enforce uniqueness and merge definitions.
    # For everything else, they can refer to the base definitions but have their own scope.
    # By not swapping to a chainmap for snippet definitions, we catch interdependent snippet files -
    # those would cause problems if read in the wrong order.
    snippet_dicts: list[dict[str, Snippet[Any]]] = []
    if not is_snippet:
        for attr_name, disp_name in SNIPPET_KINDS:
            snip_map: dict[str, Snippet[Any]] = {}
            snippet_dicts.append(snip_map)
            setattr(file_fgd, attr_name, ChainMap(snip_map, getattr(base_fgd, attr_name)))

    file_fgd.parse_file(
        fsys,
        file,
        eval_bases=False,
        encoding='utf8',
    )
    for clsname, ent in file_fgd.entities.items():
        if clsname in base_fgd.entities:
            raise ValueError(
                f'Duplicate "{clsname}" class '
                f'in {file.path} and {ent_source[clsname.upper()]}!'
            )
        base_fgd.entities[clsname] = ent
        ent_source[clsname.upper()] = path

    if fgd_vis:
        for parent, visgroup in file_fgd.auto_visgroups.items():
            try:
                existing_group = base_fgd.auto_visgroups[parent]
            except KeyError:
                base_fgd.auto_visgroups[parent] = visgroup
            else:  # Need to merge
                existing_group.ents.update(visgroup.ents)

    base_fgd.mat_exclusions.update(file_fgd.mat_exclusions)
    for tags, mat_list in file_fgd.tagged_mat_exclusions.items():
        base_fgd.tagged_mat_exclusions[tags] |= mat_list

    dest: ChainMap[str, Snippet[Any]]
    if is_snippet:
        for attr_name, disp_name in SNIPPET_KINDS:
            dest = getattr(base_fgd, attr_name)
            for name, value in getattr(file_fgd, attr_name).items():
                if name in dest:
                    raise ValueError(f'Duplicate "{name}" {disp_name} snippet in "{path}"!')
                dest[name] = value
    elif any(snippet_dicts):
        SNIPPET_USED.update(file_fgd.entities)

    print('s' if is_snippet else '.', end='', flush=True)


def get_appliesto(ent: EntityDef) -> list[str]:
    """Ensure exactly one AppliesTo() helper is present, and return the args.

    If no helper exists, one will be prepended. Otherwise, only the first
    will remain, with the arguments merged together. The same list is
    returned, so it can be viewed or edited.
    """
    found: HelperExtAppliesTo | None = None
    count = 0
    applies_to: set[str] = set()
    for i, helper in enumerate(ent.helpers):
        if isinstance(helper, HelperExtAppliesTo):
            if found is None:
                found = helper
            count += 1
            applies_to.update(helper.tags)

    if count == 1 and found is not None:
        # A single one found, use that one.
        return found.tags

    if found is None:
        found = HelperExtAppliesTo([])
        ent.helpers.insert(0, found)
    found.tags = arg_list = [tag.upper() for tag in applies_to]
    arg_list.sort()
    ent.helpers[:] = [
        helper for helper in ent.helpers
        if helper is found or not isinstance(helper, HelperExtAppliesTo)
    ]
    return arg_list


def add_tag(tags: frozenset[str], new_tag: str) -> frozenset[str]:
    """Modify these tags such that they allow the new tag."""
    is_inverted = new_tag.startswith(('!', '-'))

    required = new_tag.startswith(('+'))
    if required:
        # Strip the + off the tag
        new_tag = new_tag[1:]

    # Already allowed/disallowed.
    if not required and match_tags(expand_tags(frozenset({new_tag})), tags) != is_inverted:
        return tags

    tag_set = set(tags)
    if is_inverted:
        tag_set.discard(new_tag[1:])
        tag_set.add(new_tag)
    else:
        tag_set.discard('!' + new_tag.upper())
        tag_set.discard('-' + new_tag.upper())
        if ('+' + new_tag.upper()) not in tag_set:
            tag_set.add(new_tag.upper())

    return frozenset(tag_set)


def check_ent_sprites(ent: EntityDef, used: dict[str, list[str]]) -> None:
    """Check if the specified entity has a unique sprite."""
    mdl: str | None = None
    sprite: str | None = None
    for helper in ent.helpers:
        if type(helper) in UNIQUE_HELPERS:
            return  # Specialised helper is sufficient.
        if isinstance(helper, fgd.HelperModel):
            if helper.model is None and 'model' in ent.kv:
                return  # Model is customisable.
            mdl = helper.model
        if isinstance(helper, fgd.HelperSprite):
            if helper.mat is None:
                print(f'{ent.classname}: {helper}???')
            sprite = helper.mat
    # If both model and sprite, allow model to be duplicate.
    if mdl and sprite:
        display = sprite
    elif mdl:
        display = mdl
    elif sprite:
        display = sprite
    else:
        if '+ENGINE' not in get_appliesto(ent):
            print(f'{ent.classname}: No sprite/model? {", ".join(map(repr, ent.helpers))}')
        return

    display = display.casefold()
    if display in used:
        print(f'{ent.classname}: Reuses {display}: {used[display]}')
    used[display].append(ent.classname)


def action_count(
    dbase: Path,
    extra_db: Path | None,
    factories_folder: Path,
) -> None:
    """Output a count of all entities in the database per game."""
    fgd, base_entity_def = load_database(dbase, extra_db)

    count_base: dict[str, int] = Counter()
    count_point: dict[str, int] = Counter()
    count_brush: dict[str, int] = Counter()

    all_tags = set()

    ent: EntityDef
    for ent in fgd:
        for tag in get_appliesto(ent):
            all_tags.add(tag.lstrip('+-!').upper())

    games = (ALL_GAMES | ALL_MODS) & all_tags

    print('Done.\nGames: ' + ', '.join(sorted(games)))

    expanded: dict[str, frozenset[str]] = {
        game: expand_tags(frozenset({game}))
        for game in ALL_GAMES | ALL_MODS
    }
    expanded['ALL'] = frozenset()

    game_classes: MutableMapping[tuple[str, str], set[str]] = defaultdict(set)
    base_uses: MutableMapping[str, set[str]] = defaultdict(set)
    all_ents: MutableMapping[str, set[str]] = defaultdict(set)

    kv_counts: dict[tuple, list[tuple]] = defaultdict(list)
    inp_counts: dict[tuple, list[tuple]] = defaultdict(list)
    out_counts: dict[tuple, list[tuple]] = defaultdict(list)
    desc_counts: dict[tuple, list[tuple]] = defaultdict(list)
    val_list_counts: dict[tuple, list[tuple]] = defaultdict(list)

    for ent in fgd:
        if ent.type is EntityTypes.BASE:
            counter = count_base
            typ = 'Base'
            # Ensure it's present, so we detect 0-use bases.
            base_uses[ent.classname]  # noqa
        elif ent.type is EntityTypes.BRUSH:
            counter = count_brush
            typ = 'Brush'
        else:
            counter = count_point
            typ = 'Point'
        appliesto = get_appliesto(ent)

        has_ent = set()

        for base in ent.bases:
            assert isinstance(base, EntityDef)
            base_uses[base.classname].add(ent.classname)

        for game, tags in expanded.items():
            if match_tags(tags, appliesto):
                counter[game] += 1
                game_classes[game, typ].add(ent.classname)
                has_ent.add(game)
            # Allow explicitly saying certain ents aren't in the actual game
            # with the "engine" tag, or only adding them to this + the binary dump.
            if ent.type is not EntityTypes.BASE and match_tags(tags | {'ENGINE'}, appliesto):
                all_ents[game].add(ent.classname.casefold())

        has_ent.discard('ALL')

        if has_ent == games:
            # Applies to all, strip.
            game_classes['ALL', typ].add(ent.classname)
            counter['ALL'] += 1
            if appliesto:
                print('ALL game: ', ent.classname)
            for game in games:
                counter[game] -= 1
                game_classes[game, typ].discard(ent.classname)

        if ent.classname in SNIPPET_USED:
            # This entity does use snippets already, don't count it.
            continue

        for name, kv_map in ent.keyvalues.items():
            for tags, kv in kv_map.items():
                if 'ENGINE' in tags or kv.type is ValueTypes.SPAWNFLAGS:
                    continue
                if kv.desc:  # Blank is not a duplicate!
                    desc_counts[kv.desc, ].append((ent.classname, name))
                kv_counts[
                    kv.name, kv.type, (tuple(kv.val_list) if kv.val_list is not None else ()), kv.desc, kv.default,
                ].append((ent.classname, name, kv.desc))
                if kv.val_list is not None:
                    val_list_counts[tuple(kv.val_list)].append((ent.classname, name))
        for name, io_map in ent.inputs.items():
            for tags, io in io_map.items():
                if 'ENGINE' in tags:
                    continue
                inp_counts[io.name, io.type, io.desc].append((ent.classname, name, io.desc))
        for name, io_map in ent.outputs.items():
            for tags, io in io_map.items():
                if 'ENGINE' in tags:
                    continue
                out_counts[io.name, io.type, io.desc].append((ent.classname, name, io.desc))

    all_games: set[str] = {*count_base, *count_point, *count_brush}

    def ordering(game: str) -> tuple:
        """Put ALL at the start, mods at the end."""
        if game == 'ALL':
            return (0, 0)
        try:
            return (1, GAME_ORDER.index(game))
        except ValueError:
            return (2, game)  # Mods

    game_order = sorted(all_games, key=ordering)

    row_temp = '{:^9} | {:^6} | {:^6} | {:^6}'
    header = row_temp.format('Game', 'Base', 'Point', 'Brush')

    print(header)
    print('-' * len(header))

    for game in game_order:
        print(row_temp.format(
            game,
            count_base[game],
            count_point[game],
            count_brush[game],
        ))

    print('\n\nBases:')
    for base, count in sorted(base_uses.items(), key=lambda x: (len(x[1]), x[0])):
        ent = fgd[base]
        if ent.type is EntityTypes.BASE and (
            ent.keyvalues or ent.outputs or ent.inputs
        ):
            print(base, len(count), count if len(count) == 1 else '...')

    print('\n\nEntity Dumps:')
    for dump_path in factories_folder.glob('*.txt'):
        with dump_path.open() as f:
            dump_classes = {
                cls.casefold().strip()
                for cls in f
                if not cls.isspace()
            }
        game = dump_path.stem.upper()
        tags = frozenset(game.split('_'))

        defined_classes = {
            cls
            for tag in tags
            for cls in all_ents.get(tag, ())
            if not cls.startswith('comp_')
        }
        if not defined_classes:
            print(f'No dump for tags "{game}"!')
            continue

        extra = defined_classes - dump_classes
        missing = dump_classes - defined_classes
        if extra:
            print(f'{game} - Extraneous definitions: ')
            print(', '.join(sorted(extra)))
        if missing:
            print(f'{game} - Missing definitions: ')
            print(', '.join(sorted(missing)))

    print('\n\nMissing Class Resources:')

    missing_count = 0
    defined_count = 0
    not_in_engine = {'-ENGINE', '!ENGINE', 'SRCTOOLS', '+SRCTOOLS'}
    for clsname in sorted(fgd.entities):
        ent = fgd.entities[clsname]
        if ent.type is EntityTypes.BASE:
            continue

        if not not_in_engine.isdisjoint(get_appliesto(ent)):
            continue
        if isinstance(ent.resources, tuple):
            print(clsname, end=', ')
            missing_count += 1
        else:
            defined_count += 1

    print(
        f'\nMissing: {missing_count}, '
        f'Defined: {defined_count} = {defined_count/(missing_count + defined_count):.2%}\n\n'
    )

    mdl_or_sprite: dict[str, list[str]] = defaultdict(list)
    for ent in fgd:
        if ent.type is not EntityTypes.BASE and ent.type is not EntityTypes.BRUSH:
            check_ent_sprites(ent, mdl_or_sprite)

    for kind_name, count_map in (
        ('keyvalues', kv_counts),
        ('inputs', inp_counts),
        ('outputs', out_counts),
        ('val list', val_list_counts),
        ('desc', desc_counts)
    ):
        print(f'Duplicate {kind_name}:')
        for key, info in sorted(count_map.items(), key=lambda v: len(v[1]), reverse=True):
            if len(info) <= 2:
                continue
            print(f'{len(info):02}: {key[:64]!r} -> {info}')


def action_import_find_matching_tag(tag_map, expanded):
    found_tag = None
    found_value = None
    applies_to_current = False
    applies_to_engine = False
    for tag, value in tag_map.items():
        if 'ENGINE' in tag or '+ENGINE' in tag:
            # Ignoring all values with ENGINE tags!
            applies_to_engine = True
            continue
        
        # Take this value for now
        # Only break if we found something that matches exactly our tag
        # Otherwise keep searching, as we might find a better match later on
        found_tag = tag
        found_value = value
        applies_to_current = match_tags(expanded, tag)
        if applies_to_current:
            break
    return [found_tag, found_value, applies_to_current, applies_to_engine]

def action_import_check_for_base(fgd, ent, new_base):
    if new_base in ent.bases:
        return True

    # Not immediately present. Recursively check if it's in one of the base classes
    for base in ent.bases:
        print(base)
        base_ent = None
        try:
            base_ent = fgd[base]
        except KeyError:
            print(f'Base {base} not found!')
        if base_ent != None:
            if action_import_check_for_base(fgd, base_ent, new_base):
                return True
    return False


def action_import(
    dbase: Path,
    engine_tag: str,
    fgd_paths: list[Path],
) -> None:
    """Import an FGD file, adding differences to the unified files."""
    new_fgd = FGD()
    print(f'Using tag "{engine_tag}"')

    expanded = expand_tags(frozenset({engine_tag}))

    print('Reading new FGDs:')
    for path in fgd_paths:
        print(path)
        with RawFileSystem(str(path.parent)) as fsys:
            new_fgd.parse_file(fsys, fsys[path.name], eval_bases=False)


    print('Reading original FGDs:')
    old_fgd = FGD()
    old_ent_source: dict[str, str] = {}

    # First, load all snippets files
    with RawFileSystem(str(dbase)) as fsys:
        for file in dbase.rglob("snippets/*.fgd"):
            rel_loc = file.relative_to(dbase)
            load_file(old_fgd, old_ent_source, fsys, fsys[str(rel_loc)], is_snippet=True, fgd_vis=True)
        # Then, everything else.
        for file in dbase.rglob("*.fgd"):
            rel_loc = file.relative_to(dbase)
            if 'snippets' not in rel_loc.parts:
                load_file(old_fgd, old_ent_source, fsys, fsys[str(rel_loc)], is_snippet=False, fgd_vis=True)


    print(f'\nImporting {len(new_fgd)} entiti{"y" if len(new_fgd) == 1 else "ies"}...')
    for new_ent in new_fgd:

        print('')
        print('========================')
        print(new_ent.classname)
        print('========================')
        
        has_changes = False
        is_new = False
        ent = None
        try:
            ent = old_fgd[new_ent.classname]
        except KeyError:
            print(f'Classname not present in original FGD: "{new_ent.classname}"!')

        # Now merge the two.
        if ent != None:
            if new_ent.desc not in ent.desc:
                # Temporary, append it.
                ent.desc += '|||' + new_ent.desc
                has_changes = True

            # Merge bases. We just combine overall...
            for new_base in new_ent.bases:
                # HACK: Certain refactors to base classes in the HA FGD cause common base classes in non-HA derived FGDs to produce lots of nonsense diffs
                #       We'll reroute this to their new merged up names
                if new_base in ["Targetname", "Parentname"]:
                    new_base = "BaseEntityPoint"
                if new_base in ["RenderFxChoices"]:
                    new_base = "RenderFields"
                if new_base in ["Shadow", "Studiomodel"]:
                    new_base = "BaseEntityAnimating" # Could alternatively map up to BaseEntityPhysics in some cases!
                if new_base in ["Breakable"]:
                    new_base = "_Breakable"
                # With the fixup, sometimes we can set a base that is the same as our own name, causing recursion. Make sure we don't add it!
                if new_base == ent.classname:
                    continue

                # HACK: Effectively merged into BaseEntityPoint, but still exists
                if new_base in ["Angles"] and action_import_check_for_base(old_fgd, ent, "BaseEntityPoint"):
                    continue

                # Check if the new base is present in anywhere in the inheritance tree
                if not action_import_check_for_base(old_fgd, ent, new_base):
                    ent.bases.append(new_base)
                    has_changes = True

            # Merge helpers. We just combine overall...
            for helper in new_ent.helpers:
                # Sorta ew, quadratic search. But helper sizes shouldn't
                # get too big.
                if helper not in ent.helpers:
                    ent.helpers.append(helper)
                    has_changes = True
                        

            # Scan over all fields in the FGD entry
            for cat in ('keyvalues', 'inputs', 'outputs'):
                cur_map: dict[str, dict[frozenset[str], EntityDef]] = getattr(ent, cat)
                new_map = getattr(new_ent, cat)
                new_names = set()
                for name, tag_map in new_map.items():
                    new_names.add(name)
                    try:
                        orig_tag_map = cur_map[name]
                    except KeyError:
                        # Not present in the old FGD entry
                        # Check if it's present in one of the base classes. If it is, we'll just have to skip it
                        found_valid_base = False
                        for base in ent.bases:
                            base_ent = None
                            try:
                                base_ent = old_fgd[base]
                            except KeyError:
                                print(f'Base {base} not found!')
                            if base_ent != None:
                                base_map: dict[str, dict[frozenset[str], EntityDef]] = getattr(base_ent, cat)
                                for base_name, base_tag_map in base_map.items():
                                    if base_name.upper() != name.upper():
                                        continue
                                    # Find the tag that best matches what we have
                                    [found_base_tag, found_base_value, applies_to_current, applies_to_engine] = action_import_find_matching_tag(base_tag_map, expanded)
                                    if applies_to_current:
                                        found_valid_base = True
                                        break
                            if found_valid_base:
                                break

                        if found_valid_base:
                            # Skip without changes
                            # No reasonable way to update the base class from where we are at the moment
                            print(f"Found base! Skipping: {name}")
                            continue

                        print(f'[New] {name}')
                        cur_map[name] = {
                            add_tag(tag, '+' + engine_tag): value
                            for tag, value in tag_map.items()
                        }
                        has_changes = True
                        continue

                    # Otherwise merge, if unequal add the new ones.
                    # TODO: Handle tags in "new" files.
                    for tag, new_value in tag_map.items():
                        
                        # Find the tag that best matches what we have
                        [old_tag, old_value, old_applies_to_current, old_applies_to_engine] = action_import_find_matching_tag(orig_tag_map, expanded)

                        if old_value == None:
                            # It's new, just add it in unless it's an engine field
                            if old_applies_to_engine:
                                print(f'[Engine] {new_value.name}')
                            else:
                                print(f'[New] {new_value.name}')
                                orig_tag_map[add_tag(tag, '+' + engine_tag)] = new_value
                                has_changes = True
                        else:
                            # Diff check!
                            
                            # if the old has a better desc, take it as our own
                            if len(new_value.desc) == 0 and len(old_value.desc) != 0:
                                print(f'Taking old desc: "{old_value.desc}" -> "{new_value.desc}"')
                                new_value.desc = old_value.desc
                                if cat == 'keyvalues':
                                    print(f'Taking old disp_name: "{old_value.disp_name}" -> "{new_value.disp_name}"')
                                    new_value.disp_name = old_value.disp_name

                            if old_value.name != new_value.name:
                                print(f'Name fixup! "{old_value.name}" -> "{new_value.name}"')
                            new_value.name = old_value.name

                            # Same value?
                            if old_value == new_value:
                                print(f'[Same] {old_value.name}')
                                continue
                            
                            # Don't whack booleans with choices!
                            if old_value.type == ValueTypes.BOOL and (new_value.type == ValueTypes.CHOICES and len(new_value.val_list) <= 2):
                                print(f'[Note] {new_value.name} : Ignore downgrade {old_value.type} -> {new_value.type}')
                                continue

                            # Ignore downgrades to int from choices
                            if old_value.type == ValueTypes.CHOICES and new_value.type == ValueTypes.INT:
                                print(f'[Note] {new_value.name} : Ignore downgrade {old_value.type} -> {new_value.type}')
                                continue
                            
                            # Ignore downgrades to strings
                            if old_value.type != ValueTypes.STRING and new_value.type == ValueTypes.STRING:
                                print(f'[Note] {new_value.name} : Ignore downgrade {old_value.type} -> {new_value.type}')
                                continue
                                
                            # Ignore downgrade from filter to generic
                            if old_value.type == ValueTypes.TARG_FILTER_NAME and new_value.type == ValueTypes.TARG_DEST:
                                print(f'[Note] {new_value.name} : Ignore downgrade {old_value.type} -> {new_value.type}')
                                continue

                            # Ignore downgrades to generic targetname 
                            if new_value.type == ValueTypes.TARG_DEST:
                                a = [ValueTypes.TARG_DEST_CLASS, ValueTypes.TARG_SOURCE, ValueTypes.TARG_NPC_CLASS, ValueTypes.TARG_POINT_CLASS, ValueTypes.TARG_FILTER_NAME, ValueTypes.TARG_NODE_DEST, ValueTypes.TARG_NODE_SOURCE]
                                if old_value.type in a:
                                    print(f'[Note] {new_value.name} : Ignore downgrade {old_value.type} -> {new_value.type}')
                                    continue
                            
                            # Check if type, description, name, default, etc. is any different
                            has_some_diff = False
                            dont_replace = False
                            if old_value.type != new_value.type or old_value.desc != new_value.desc:
                                has_some_diff = True
                            elif cat == 'keyvalues':
                                if old_value.disp_name != new_value.disp_name or old_value.readonly != new_value.readonly or old_value.reportable != new_value.reportable:
                                    has_some_diff = True

                                # Check for if the default value is the same
                                od = old_value.default
                                nd = new_value.default
                                if old_value.type == ValueTypes.INT or new_value.type == ValueTypes.INT or old_value.type == ValueTypes.FLOAT or new_value.type == ValueTypes.FLOAT:
                                    # Strip leading or trailing 0's and trailing .'s
                                    od = od.strip('0').rstrip('.')
                                    nd = nd.strip('0').rstrip('.')
                                if od == "":
                                    od = None
                                if nd == "":
                                    nd = None
                                if od != nd:
                                    has_some_diff = True
                                    dont_replace = True # For different default values, tack them on as a separate field with a requirement tag

                            # WIP: Value List diff check
                            if not has_some_diff and cat == 'keyvalues' and (old_value.val_list != None and new_value.val_list != None) and (len(old_value.val_list) > 0 or len(new_value.val_list) > 0):
                                # Need to check the value lists and see if they're any different
                                for new_tupple in new_value.val_list:
                                    new_val = new_tupple[0]
                                    new_name = new_tupple[1]
                                    new_val_tags = new_tupple[-1]
                                    for old_tupple in old_value.val_list:
                                        old_val = old_tupple[0]
                                        old_name = old_tupple[1]
                                        old_val_tags = old_tupple[-1]
                                        if new_val == old_val:
                                            if old_val_tags < new_val_tags:
                                                #print(f'DIFFERENT tags! "{old_val_tags}" -> "{new_val_tags}"')
                                                has_some_diff = True
                                            elif len(new_tupple) == 4 and len(old_tupple) == 4:
                                                new_default = new_tupple[2]
                                                old_default = old_tupple[2]
                                                if new_default != old_default:
                                                    print(f'DIFFERENT default! "{old_default}" -> "{new_default}"')
                                                    has_some_diff = True
                                            break
                                    else:
                                        print(f'not found? {new_name}')
                                        has_some_diff = True
                                        
                            
                            if not has_some_diff:
                                # Despite initially thinking we had a difference here, the end result is close enough to be considered the same
                                print(f'[CloseEnough] {old_value.name}')
                                continue
                            
                            print('Difference:')
                            print('\t', old_value)
                            print('\t', new_value)
                            
                            if dont_replace:
                                # We'll want to disable the original field for our entry and add a new separate field for it
                                del orig_tag_map[old_tag]
                                orig_tag_map[add_tag(old_tag, '!' + engine_tag)] = old_value
                                orig_tag_map[add_tag(tag, '+' + engine_tag)] = new_value

                            else:
                                # Already present, modify this tag.
                                del orig_tag_map[old_tag]
                                if old_applies_to_current:
                                    orig_tag_map[old_tag] = new_value
                                else:
                                    orig_tag_map[add_tag(old_tag, engine_tag)] = new_value
                            has_changes = True


                ## Make sure removed items don't apply to the new tag.
                #for name, tag_map in cur_map.items():
                #    if name not in new_names:
                #        cur_map[name] = {
                #            add_tag(tag, '!' + engine_tag): value
                #            for tag, value in tag_map.items()
                #        }

        else:
            # No existing one, just set appliesto.
            ent = new_ent
            has_changes = True
            is_new = True


        applies_to = get_appliesto(ent)
        if not match_tags(expanded, applies_to):
            applies_to.append(engine_tag)
            has_changes = True
        ent.helpers[:] = [
            helper for helper in ent.helpers
            if not isinstance(helper, HelperExtAppliesTo)
        ]

        # Print status and skip unchanged entries
        if not has_changes:
            print('Result: Unchanged')
            continue
        if is_new:
            print('Result: New Entry')
        else:
            print('Result: Modified Entry')

        # Write changes to disk
        # Use the original path for existing entities or a new path for new entities
        path = ""
        if is_new:
            path = dbase / ent_path(new_ent)
        else:
            path = dbase / old_ent_source[ent.classname.upper()]
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf8') as f:
            ent.export(f)

    print()


def action_export(
    dbase: Path,
    extra_db: Path | None,
    tags: frozenset[str],
    output_path: Path,
    as_binary: bool,
    engine_mode: bool,
    map_size: int = MAP_SIZE_DEFAULT,
    srctools_only: bool = False,
    collapse_bases: bool = False
) -> None:
    """Create an FGD file using the given tags."""

    if engine_mode:
        tags = frozenset({'ENGINE'})
    else:
        tags = expand_tags(tags)

    if srctools_only:
        tags |= {'SRCTOOLS'}
        srctools_tags = tags - {'SRCTOOLS'}
    else:
        srctools_tags = None

    print('Tags expanded to: {}'.format(', '.join(tags)))

    fgd, base_entity_def = load_database(dbase, extra_loc=extra_db, map_size=map_size)

    print(f'Map size: ({fgd.map_size_min}, {fgd.map_size_max})')

    aliases: dict[EntityDef, str | EntityDef] = {}
    if engine_mode or collapse_bases:
        # In engine mode, we don't care about specific games.
        print('Collapsing bases...')
        for ent in fgd:
            if ent.is_alias:
                try:
                    [base] = ent.bases
                except ValueError as exc:
                    raise ValueError('Bad alias base for: ' + ent.classname) from exc
                aliases[ent] = base
        fgd.collapse_bases()

    if engine_mode:
        print('Merging tags...')
        for ent in list(fgd):
            # We want to include not-in-engine entities like func_detail still, for parsing
            # VMFs and the like.

            # Strip applies-to helper and ordering helper.
            ent.helpers[:] = [
                helper for helper in ent.helpers
                if not helper.IS_EXTENSION
            ]
            # Force everything to inherit from CBaseEntity, since
            # we're then removing any KVs that are present on that.
            if ent.is_alias:
                ent.bases = [aliases[ent]]
            elif ent.classname != BASE_ENTITY:
                ent.bases = [base_entity_def]

            value: EntAttribute
            category: dict[str, dict[frozenset[str], EntAttribute]]
            base_cat: dict[str, dict[frozenset[str], EntAttribute]]
            for attr_name in ['inputs', 'outputs', 'keyvalues']:
                # Unsafe cast, we're not going to insert the wrong kind of attribute though.
                category = getattr(ent, attr_name)
                base_cat = getattr(base_entity_def, attr_name)
                # For each category, check for what value we want to keep.
                # If only one, we keep that.
                # If there's an "ENGINE" tag, that's specifically for us.
                # Otherwise, warn if there's a type conflict.
                # If the final value is choices, warn too (not really a type).
                for key, orig_tag_map in list(category.items()):
                    # Remake the map, excluding non-engine tags.
                    # If any are explicitly matching us, just use that
                    # directly.
                    tag_map: dict[frozenset[str], EntAttribute] = {}
                    for tags, value in orig_tag_map.items():
                        if 'ENGINE' in tags or '+ENGINE' in tags:
                            if value.type is ValueTypes.CHOICES:
                                raise ValueError(
                                    f'{ent.classname}.{key}: Engine tags cannot be CHOICES!'
                                )
                            # Use just this.
                            tag_map = {TAGS_EMPTY: value}
                            break
                        elif '-ENGINE' not in tags and '!ENGINE' not in tags:
                            tag_map[tags] = value

                    if not tag_map:
                        # All were set as non-engine, so it's not present.
                        del category[key]
                        continue
                    elif len(tag_map) == 1:
                        # Only one type, that's the one for the engine.
                        [value] = tag_map.values()
                    else:
                        # More than one tag.
                        # IODef and KeyValues have a type attr.
                        types = {val.type for val in tag_map.values()}
                        if len(types) > 1:
                            print('{}.{} has multiple types! ({})'.format(
                                ent.classname,
                                key,
                                ', '.join([typ.value for typ in types])
                            ))
                        # Pick the one with the shortest tags arbitrarily.
                        _, value = min(
                            tag_map.items(),
                            key=lambda t: len(t[0]),
                        )

                    # If it's CHOICES, we can't know what type it is.
                    # Guess either int or string, if we can convert.
                    if value.type is ValueTypes.CHOICES:
                        print(
                            f'{ent.classname}.{key} uses CHOICES type, '
                            'provide ENGINE tag!'
                        )
                        if isinstance(value, KVDef):
                            assert value.val_list is not None
                            try:
                                for choice_val, name, tagset in value.choices_list:
                                    int(choice_val)
                            except ValueError:
                                # Not all are ints, it's a string.
                                value.type = ValueTypes.STRING
                            else:
                                value.type = ValueTypes.INT
                            value.val_list = None

                    # Check if this is a shared property among all ents,
                    # and if so skip exporting.
                    if ent.classname != BASE_ENTITY:
                        base_value: EntAttribute
                        try:
                            [base_value] = base_cat[key].values()
                        except KeyError:
                            pass
                        except ValueError:
                            raise ValueError(
                                f'Base Entity {attr_name[:-1]} "{key}" '
                                f'has multiple tags: {list(base_cat[key].keys())}'
                            ) from None
                        else:
                            if base_value.type is ValueTypes.CHOICES:
                                print(
                                    f'Base Entity {attr_name[:-1]} '
                                    f'"{key}"  is a choices type!'
                                )
                            elif base_value.type is value.type:
                                del category[key]
                                continue
                            elif attr_name == 'keyvalues' and key == 'model':
                                # This can be sprite or model.
                                pass
                            elif base_value.type is ValueTypes.FLOAT and value.type is ValueTypes.INT:
                                # Just constraining it down to a whole number.
                                pass
                            elif attr_name != 'keyvalues' and base_value.type is ValueTypes.VOID:
                                # Base ignores parameters, but child has some - that's fine.
                                pass
                            else:
                                print(f'{ent.classname}.{key}: {value.type} != base {base_value.type}')

                    # Blank this, it's not that useful.
                    value.desc = ''
                    category[key] = {TAGS_EMPTY: value}

        # Add in the base entity definition, and clear it out.
        fgd.entities[BASE_ENTITY.casefold()] = base_entity_def
        base_entity_def.desc = ''
        base_entity_def.helpers = []
        # Strip out all the tags.
        for attr_name in ['inputs', 'outputs', 'keyvalues']:
            # Unsafe cast, we're not going to insert the wrong kind of attribute though.
            category = getattr(base_entity_def, attr_name)
            for key, tag_map in category.items():
                [value] = tag_map.values()
                category[key] = {TAGS_EMPTY: value}
                if value.type is ValueTypes.CHOICES:
                    raise ValueError('Choices key in CBaseEntity!')
    else:
        print('Culling incompatible entities...')

        ents = list(fgd.entities.values())
        fgd.entities.clear()

        for ent in ents:
            applies_to = get_appliesto(ent)
            if match_tags(tags, applies_to):
                # For the srctools_only flag, only allow ents which match the regular
                # tags, but do not match those minus srctools.
                if srctools_tags is not None and match_tags(srctools_tags, applies_to):
                    # Always include base entities, those get culled later.
                    # _CBaseEntity_ is required for engine definitions.
                    if ent.type is not EntityTypes.BASE and ent is not base_entity_def:
                        continue

                fgd.entities[ent.classname] = ent
                ent.strip_tags(tags)

            # Remove bases that don't apply.
            for base in ent.bases[:]:
                assert isinstance(base, EntityDef)
                if not match_tags(tags, get_appliesto(base)):
                    ent.bases.remove(base)

    if not engine_mode:
        print('Applying polyfills:')
        for poly_tag, polyfill in POLYFILLS:
            if match_tags(tags, poly_tag):
                print(f' - {polyfill.__name__[10:]}: Applying')
                polyfill(fgd)
            else:
                print(f' - {polyfill.__name__[10:]}: Not required')

    print('Applying helpers to child entities and optimising...')
    for ent in fgd.entities.values():
        # Merge them together.
        base_helpers: list[Helper] = []
        for base in ent.bases:
            assert isinstance(base, EntityDef)
            base_helpers.extend(base.helpers)

        # Then optimise this list, by re-assembling in reverse.
        rev_helpers: list[Helper] = []
        overrides: set[HelperTypes] = set()

        # Add the entity's own helpers to the end, but do not override within that.
        for helper in reversed(ent.helpers):
            if helper in rev_helpers:  # No duplicates here.
                continue
            if helper.IS_EXTENSION:
                continue

            # For each, it may make earlier definitions obsolete.
            overrides.update(helper.overrides())
            # But the last of any type is always included.
            rev_helpers.append(helper)

        # Add in all the base entity helpers.
        for helper in reversed(base_helpers):
            # No duplicates or overridden helpers.
            if helper in rev_helpers or helper.TYPE in overrides:
                continue
            if helper.IS_EXTENSION:
                continue
            overrides.update(helper.overrides())
            rev_helpers.append(helper)
        ent.helpers = rev_helpers[::-1]

    print('Culling unused bases...')
    used_bases: set[EntityDef] = set()
    # We only want to keep bases that provide keyvalues. We've merged the
    # helpers in.
    for ent in fgd.entities.values():
        if ent.type is not EntityTypes.BASE:
            for base in ent.iter_bases():
                if base.type is EntityTypes.BASE and (
                    base.keyvalues or base.inputs or base.outputs
                ):
                    used_bases.add(base)

    for classname, ent in list(fgd.entities.items()):
        if ent.type is EntityTypes.BASE:
            if ent not in used_bases and ent is not base_entity_def:
                del fgd.entities[classname]
                continue
            else:
                # Helpers aren't inherited, so this isn't useful any more.
                ent.helpers.clear()
        # Cull all base classes we don't use.
        # Ents that inherit from each other always need to exist.
        # We also need to replace bases with their parent, if culled.
        todo = ent.bases.copy()
        done = set(todo)
        ent.bases.clear()
        for base in todo:
            assert isinstance(base, EntityDef), base
            if base.type is not EntityTypes.BASE or base in used_bases:
                ent.bases.append(base)
            else:
                for subbase in base.bases:
                    if subbase not in done:
                        todo.append(subbase)

    print('Merging in material exclusions...')
    for mat_tags, materials in fgd.tagged_mat_exclusions.items():
        if match_tags(tags, mat_tags):
            fgd.mat_exclusions |= materials
    fgd.tagged_mat_exclusions.clear()

    print('Culling visgroups...')
    # Cull visgroups that no longer exist for us.
    valid_ents = {
        ent.classname.casefold()
        for ent in fgd.entities.values()
        if ent.type is not EntityTypes.BASE
    }
    for key, visgroup in list(fgd.auto_visgroups.items()):
        visgroup.ents.intersection_update(valid_ents)
        if not visgroup.ents:
            del fgd.auto_visgroups[key]

    if engine_mode:
        res_tags: dict[str, set[str]] = defaultdict(set)
        for ent in fgd.entities.values():
            for res in ent.resources:
                for tag in res.tags:
                    res_tags[tag.lstrip('-+!').upper()].add(ent.classname)
        print('Resource tags:')
        for tag, classnames in res_tags.items():
            print(f'- {tag}: {len(classnames)} ents')

    print(f'Exporting {output_path}...')

    if as_binary:
        with open(output_path, 'wb') as bin_f:
            # Private, reserved for us.
            from srctools._engine_db import serialise  # noqa
            serialise(fgd, bin_f)
    else:
        with open(output_path, 'w', encoding='iso-8859-1') as txt_f:
            fgd.export(txt_f)
            # BEE2 compatibility, don't make it run.
            if 'P2' in tags:
                txt_f.write('\n// BEE 2 EDIT FLAG = 0 \n')


def action_visgroup(dbase: Path, extra_loc: Path | None, dest: Path) -> None:
    """Dump all auto-visgroups into the specified file, using a custom format."""
    fgd, base_entity_def = load_database(dbase, extra_loc, fgd_vis=True)

    # TODO: This shouldn't be copied from fgd.export(), need to make the
    #  parenting invariant guaranteed by the classes.
    vis_by_parent: dict[str, set[AutoVisgroup]] = defaultdict(set)

    for visgroup in list(fgd.auto_visgroups.values()):
        if not visgroup.parent:
            visgroup.parent = 'Auto'
        elif visgroup.parent.casefold() not in fgd.auto_visgroups:
            # This is an "orphan" visgroup, not linked back to Auto.
            # Connect it back there, by generating the parent.
            parent_group = fgd.auto_visgroups[visgroup.parent.casefold()] = AutoVisgroup(visgroup.parent, 'Auto')
            parent_group.ents.update(visgroup.ents)
        vis_by_parent[visgroup.parent.casefold()].add(visgroup)

    def write_vis(group: AutoVisgroup, indent: str) -> None:
        """Write a visgroup and its children."""
        children = sorted(vis_by_parent[group.name.casefold()], key=lambda g: g.name)
        # Special case for singleton visgroups - no children, only 1 ent.
        if not children and len(group.ents) == 1:
            [single_ent] = group.ents
            f.write(f'{indent}- {group.name} (`{single_ent}`)\n')
            return

        # First, write the child visgroups.
        child_indent = indent + '\t'
        f.write(f'{indent}- {group.name}\n')
        for child_group in children:
            write_vis(child_group, child_indent)
        # Then the actual children.
        for child in sorted(group.ents):
            # Visgroups are also in the list.
            if child in fgd.auto_visgroups:
                continue
            # For ents in subfolders, each parent group also lists
            # them. So we want to add it to the group whose children
            # do not contain the ent.
            if all(child not in group.ents for group in children):
                f.write(f'{child_indent}* `{child}`\n')

    print('Writing...')
    with dest.open('w') as f:
        write_vis(AutoVisgroup('Auto', ''), '')


def main(args: list[str] | None = None) -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Manage a set of unified FGDs, sharing configs between engine versions.",
    )
    # Find the repository root.
    repo_dir = Path(__file__).parents[2]

    parser.add_argument(
        "-d", "--database",
        default=str(repo_dir / "fgd"),
        help="The folder to write the FGD files to or from."
    )
    parser.add_argument(
        "--extra",
        dest="extra_db",
        default=None,
        help="If specified, an additional folder to read FGD files from. "
             "These override the normal database.",
    )
    subparsers = parser.add_subparsers(dest="mode")

    subparsers.add_parser(
        "count",
        help=action_count.__doc__,
        aliases=["c"],
    )

    parser_exp = subparsers.add_parser(
        "export",
        help=action_export.__doc__,
        aliases=["exp", "e"],
    )

    parser_exp.add_argument(
        "-o", "--output",
        default="output.fgd",
        help="Destination FGD filename.",
    )
    parser_exp.add_argument(
        "-e", "--engine",
        action="store_true",
        help='If set, produce FGD for parsing by script. '
             'This includes all keyvalues regardless of tags, '
             'to allow parsing VMF/BSP files. Overrides tags if '
             ' provided.',
    )
    parser_exp.add_argument(
        "-b", "--binary",
        action="store_true",
        help="If set, produce a binary format used by Srctools.",
    )
    parser_exp.add_argument(
        "tags",
        nargs="*",
        help="Tags to include in the output.",
        default=None,
    )
    parser_exp.add_argument(
        "--map-size",
        default=MAP_SIZE_DEFAULT,
        dest="map_size",
        type=int,
    )
    parser_exp.add_argument(
        "--collapse_bases",
        default=False,
        action="store_true",
        help='Collapse base classes and merge them into the entity definitions.',
    )
    parser_exp.add_argument(
        "--srctools_only",
        default=False,
        action="store_true",
        help='Export "comp" entities.',
    )

    parser_imp = subparsers.add_parser(
        "import",
        help=action_import.__doc__,
        aliases=["imp", "i"],
    )
    parser_imp.add_argument(
        "engine",
        type=lambda s: s.upper(),
        choices=GAME_ORDER + list(ALL_MODS),
        help="Engine to mark this FGD set as supported by.",
    )
    parser_imp.add_argument(
        "fgd",
        nargs="+",
        type=Path,
        help="The FGD files to import. "
    )

    parser_vis = subparsers.add_parser(
        "visgroup",
        help=action_visgroup.__doc__,
        aliases=["vis", "v"],
    )

    parser_vis.add_argument(
        "-o", "--output",
        default="visgroups.md",
        type=Path,
        help="Visgroup dump filename.",
    )

    result = parser.parse_args(args)

    if result.mode is None:
        parser.print_help()
        return

    dbase = Path(result.database).resolve()
    dbase.mkdir(parents=True, exist_ok=True)

    extra_db: Path | None
    if result.extra_db is not None:
        extra_db = Path(result.extra_db).resolve()
    else:
        extra_db = None

    if result.mode in ("import", "imp", "i"):
        action_import(
            dbase,
            result.engine,
            result.fgd,
        )
    elif result.mode in ("export", "exp", "e"):
        # Engine means tags are ignored.
        # Non-engine means tags must be specified!
        if result.engine:
            if result.tags:
                print("Tags ignored in --engine mode...", file=sys.stderr)
            result.tags = ['ENGINE']
        elif not result.tags:
            parser.error("At least one tag must be specified!")

        tags = validate_tags(result.tags)

        for tag in tags:
            if tag not in ALL_TAGS:
                parser.error(f'Invalid tag "{tag}"! Allowed tags: \n{format_all_tags()}')
        action_export(
            dbase,
            extra_db,
            tags,
            Path(result.output).resolve(),
            result.binary,
            result.engine,
            result.map_size,
            result.srctools_only,
            result.collapse_bases,
        )
    elif result.mode in ("c", "count"):
        action_count(dbase, extra_db, factories_folder=Path(repo_dir, 'db', 'factories'))
    elif result.mode in ("visgroup", "v", "vis"):
        action_visgroup(dbase, extra_db, result.output)
    else:
        raise AssertionError(f'Unknown mode! ({result.mode})')


if __name__ == '__main__':
    main(sys.argv[1:])
