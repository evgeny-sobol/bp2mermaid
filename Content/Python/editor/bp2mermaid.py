"""Export a Blueprint graph to Mermaid for LLM pasting.

Editor-only. Registered on the Blueprint Editor toolbar as a graph submenu.
The format writers below do not import `unreal`, so they can be tested outside the editor.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

EXPORT_SUBDIR = os.path.join('Source', 'BPMermaids')
AUTO_EXPORT_STATE_FILENAME = 'auto_export.json'
AUTO_EXPORT_DEBOUNCE_SEC = 2.0
AUTO_EXPORT_POLL_SEC = 0.35
_HOOKS_ATTR = '_bp2mermaid_hooks'
_UE_CONTENT_PATH_RE = re.compile(r'/(?:Game|Engine)(?:/[\w]+)+(?:\.\w+)?')
_SOFT_CLASS_WRAP_RE = re.compile(r"[A-Za-z_][\w.]*'(…/[^']*)'")
_K2_PREFIX_RE = re.compile(r'\bK2Node_')
_FLOAT_TOKEN_RE = re.compile(r'(?<![\w.])(-?\d+\.\d+)(?![\w.])')
_NSLOCTEXT_RE = re.compile(
    r"""NSLOCTEXT\(\s*(['"])(?:\\.|(?!\1).)*\1\s*,\s*(['"])(?:\\.|(?!\2).)*\2\s*,\s*(['"])((?:\\.|(?!\3).)*)\3\s*\)""",
)
_LEGACY_YAML_HEADER_RE = re.compile(r'\A---\r?\n.*?\r?\n---\r?\n*', re.DOTALL)
_UE_STRUCT_REPR_RE = re.compile(r"<Struct '(?P<name>[^']+)'")
_UE_STRUCT_PTR_RE = re.compile(r'\s*\(0x[0-9A-Fa-f]+\)')
_TAG_NAME_RE = re.compile(r'TagName\s*=\s*"([^"]+)"')
KNOT_CLASSES = frozenset(('K2Node_Knot', 'K2Node_Reroute'))
COMMENT_CLASSES = frozenset(('EdGraphNode_Comment',))
EXEC_PIN_CATEGORIES = frozenset(('exec', 'pc_exec'))
EXEC_PIN_NAMES = frozenset(('execute', 'then', 'exec', 'completed'))
# Graphs that only contain these (plus comments/knots already dropped from `nodes`)
# are not worth a Mermaid file. Names are after `pretty_export_text` (`K2Node_` stripped).
# Includes empty parent-call overrides (`CallParentFunction`, e.g. BP_Monitor).
ROOT_NODE_CLASSES = frozenset((
    'Event',
    'CustomEvent',
    'ComponentBoundEvent',
    'ActorBoundEvent',
    'InputAction',
    'InputActionEvent',
    'InputAxisEvent',
    'InputKey',
    'InputTouch',
    'InputAxisKeyEvent',
    'EnhancedInputAction',
    'EnhancedInputActionEvent',
    'FunctionEntry',
    'FunctionResult',
    'Tunnel',
    'AnimGraphNode_Root',
    'CallParentFunction',
))
_MERMAID_NODE_LABEL_RE = re.compile(r'^\s+n\d+\["(.*)"\]\s*$', re.MULTILINE)
_MERMAID_FENCE_RE = re.compile(r'```mermaid\s*\n(.*?)```', re.DOTALL)
_JSON_FENCE_RE = re.compile(r'```json\s*\n(.*?)```', re.DOTALL)
_MERMAID_EDGE_RE = re.compile(r'-->|-.->')
TRIVIAL_SKIP_SUFFIX = '.skip'
# Bump when the collector changes so stale `{Asset}.md.skip` from empty-Nodes
# scans (UE 5.8 graph-editor removal) do not hide real EventGraphs.
TRIVIAL_SKIP_GENERATION = '2'
CLASS_DEFAULTS_MAX_ROWS = 120
CDO_SKIP_NAMES = frozenset((
    'actor_guid', 'actor_instance_guid', 'actor_label', 'asset_user_data',
    'attach_children', 'attach_parent', 'auto_receive_input',
    'blueprint_created_components', 'children', 'content_bundle_guid',
    'controller', 'custom_time_dilation', 'data_layer_assets', 'data_layers',
    'folder_guid', 'folder_path', 'group_actor', 'hidden_editor',
    'h_lod_layer', 'initial_life_span', 'input_component', 'input_priority',
    'instance_components', 'instigator', 'is_spatially_loaded',
    'life_span', 'min_net_update_frequency', 'net_cull_distance_squared',
    'net_dormancy', 'net_driver_name', 'net_tag', 'net_update_frequency',
    'owner', 'parent_component', 'pawn', 'player_controller', 'player_state',
    'primary_actor_tick', 'replicate_movement', 'root_component',
    'runtime_grid', 'sprite_scale', 'unique_id',
))
# Inherited Class Defaults from these modules are engine/widget noise; skip before
# get_editor_property (UE Python throws on a miss — that is the export hot-path killer).
_SKIP_INHERITED_MEMBER_MARKERS = (
    '/script/engine.',
    '/script/coreuobject.',
    '/script/umg.',
    '/script/slate.',
    '/script/slatecore.',
    'engine.actor:',
    'engine.pawn:',
    'engine.character:',
    'engine.actorcomponent:',
    'engine.scenecomponent:',
    'engine.animinstance:',
)
GAS_EXPORT_PREFIXES = ('GA_', 'GE_')
DATA_ASSET_EXPORT_PREFIXES = ('DA_',)
DATA_ASSET_SKIP_NAMES = frozenset((
    'asset_import_data', 'thumbnail_info', 'asset_bundle_data',
    'package_metadata', 'simple_construction_script',
    'ubergraph_pages', 'function_graphs', 'macro_graphs',
    'delegate_signature_graphs', 'new_variables',
))
COMPONENT_SKIP_NAMES = frozenset((
    'mobility', 'creation_method', 'ucs_serialization_index',
    'replicates', 'auto_activate', 'is_editor_only',
    'editable_when_inherited', 'can_ever_affect_navigation',
    'is_active', 'physics_volume', 'body_instance',
    'component_velocity', 'bounds_scale', 'detail_mode',
    'component_tags', 'should_update_physics_volume',
    'visible', 'hidden_in_game', 'selectable',
    'use_attach_parent_bound', 'net_addressable_name',
))
COMPONENT_SKIP_PREFIXES = (
    'relative_', 'attach_', 'b_absolute_', 'b_should_snap_',
    'b_visible', 'b_hidden', 'b_render_', 'b_cast_', 'b_light_',
    'b_receive_', 'b_self_shadow', 'primary_component_tick',
    'virtual_texture', 'ray_tracing', 'translucency_sort',
    'custom_depth', 'custom_primitive', 'ld_max_draw', 'min_draw',
    'cached_max_draw', 'runtime_virtual_texture',
)
# CDO GetComponentsByClass + dir() on these types AV'd UE 5.8 Export Project.
COMPONENT_DUMP_SKIP_UNREAL_TYPES = (
    'SceneComponent',
    'MovementComponent',
    'TimelineComponent',
)
COMPONENT_DUMP_SKIP_CLASS_MARKERS = (
    'SceneComponent',
    'PrimitiveComponent',
    'MeshComponent',
    'SkinnedMesh',
    'SkeletalMesh',
    'StaticMeshComponent',
    'CapsuleComponent',
    'SphereComponent',
    'BoxComponent',
    'MovementComponent',
    'CharacterMovement',
    'SpringArm',
    'CameraComponent',
    'WidgetComponent',
    'NiagaraComponent',
    'AudioComponent',
    'LightComponent',
    'ShapeComponent',
    'ArrowComponent',
    'BillboardComponent',
    'ParticleSystem',
    'ChildActorComponent',
    'SplineComponent',
    'DecalComponent',
    'TextRenderComponent',
    'PoseableMesh',
    'AbilitySystemComponent',
    'TimelineComponent',
    'InputComponent',
    'Cloth',
    'Chaos',
)
_JSON_OMIT = object()
_EXPORT_ASSET_CLASSES = (
    ('/Script/Engine', 'Blueprint'),
    ('/Script/GameplayAbilities', 'GameplayAbilityBlueprint'),
)
GAS_CDO_EXTRA_BASES = (
    'GEComponents',
    'DurationPolicy',
    'DurationMagnitude',
    'Period',
    'StackingType',
    'StackLimitCount',
    'Modifiers',
    'Executions',
    'GameplayCues',
    'InheritableGameplayEffectTags',
    'InheritableOwnedTagsContainer',
    'OngoingTagRequirements',
    'ApplicationTagRequirements',
    'RemovalTagRequirements',
    'RemoveGameplayEffectsWithTags',
    'GrantedApplicationImmunityTags',
    'AbilityTags',
    'AssetTags',
    'ActivationOwnedTags',
    'ActivationRequiredTags',
    'ActivationBlockedTags',
    'SourceRequiredTags',
    'SourceBlockedTags',
    'TargetRequiredTags',
    'TargetBlockedTags',
    'CancelAbilitiesWithTag',
    'BlockAbilitiesWithTag',
    'ActivationPolicy',
    'NetExecutionPolicy',
    'InstancingPolicy',
    'CostGameplayEffectClass',
    'CooldownGameplayEffectClass',
    'AbilityTriggers',
)
GAS_SUBOBJECT_PROP_BASES = (
    'InheritableGrantedTags',
    'bReplicateGrantedTags',
    'InheritableAssetTags',
    'InheritableBlockedAbilityTagsContainer',
    'GrantedApplicationImmunityTags',
    'GrantedApplicationImmunityQuery',
    'ApplicationTagRequirements',
    'OngoingTagRequirements',
    'RemovalTagRequirements',
    'ApplicationRequirements',
    'ChanceToApplyToTarget',
    'OnApplicationGameplayEffects',
    'OnCompleteAlways',
    'OnCompletePrematurely',
    'OnCompleteNormal',
    'GrantAbilityConfigs',
    'RemoveGameplayEffectQueries',
    'Modifiers',
    'ModifierMagnitude',
    'Attribute',
    'ModifierOp',
)
_SKIP_CONTENT_WALK_DIRS = frozenset((
    '__externalactors__',
    '__externalobjects__',
))

_RETAIN = []
_LIST_MEMBERS_USES_FLAG = True
_LIST_MEMBERS_OUT_ARRAY = False

try:
    import unreal
except ImportError:
    unreal = None


def pretty_export_text(text):
    """Shorten UE asset paths, compact floats, and drop K2Node_ prefixes for LLM-facing labels."""
    if text is None:
        return ''
    text = str(text)
    text = _UE_CONTENT_PATH_RE.sub(_shorten_content_path, text)
    text = _SOFT_CLASS_WRAP_RE.sub(r'\1', text)
    text = _NSLOCTEXT_RE.sub(_keep_nsloctext_display, text)
    text = _compact_ue_struct_repr(text)
    text = _compact_gameplay_attribute_dumps(text)
    text = _FLOAT_TOKEN_RE.sub(lambda match: _shorten_float_token(match.group(1)), text)
    return _K2_PREFIX_RE.sub('', text)


def _looks_like_ue_struct(value):
    if value is None or isinstance(value, (str, bytes, bytearray, bool, int, float)):
        return False
    try:
        return str(value).lstrip().startswith('<Struct ')
    except Exception:
        return False


def collect_gameplay_tag_names(value):
    """Tag names from a live tag/container or a Python `<Struct>` dump string."""
    if value is None:
        return []
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:
        return []
    return _TAG_NAME_RE.findall(text)


def _format_parsed_ue_struct(name, body):
    tags = _TAG_NAME_RE.findall(body or '')
    if 'TagContainer' in name:
        return ', '.join(tags)
    if name in ('GameplayTag', 'FGameplayTag') or name.endswith('GameplayTag'):
        return tags[0] if tags else ''
    if 'GameplayEventData' in name:
        if not tags and 'TagName=' not in (body or ''):
            return 'GameplayEventData()'
    compact = re.sub(r'\s+', ' ', body or '').strip()
    compact = _UE_STRUCT_PTR_RE.sub('', compact)
    if not compact or compact in ('{}',):
        return '{}()'.format(name)
    return '{}({})'.format(name, compact)


def _compact_ue_struct_repr(text):
    """`<Struct 'GameplayTagContainer' (0x…) {TagName=…}>` → tag list / TypeName()."""
    if "<Struct '" not in text:
        return text
    guard = 0
    while "<Struct '" in text and guard < 24:
        guard += 1
        start = text.rfind("<Struct '")
        name_match = _UE_STRUCT_REPR_RE.match(text[start:])
        if not name_match:
            break
        name = name_match.group('name')
        cursor = start + name_match.end()
        ptr = _UE_STRUCT_PTR_RE.match(text[cursor:])
        if ptr:
            cursor += ptr.end()
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text) or text[cursor] != '{':
            text = text[:start] + name + '()' + text[cursor:]
            continue
        depth = 0
        close = -1
        index = cursor
        while index < len(text):
            char = text[index]
            if char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    close = index
                    break
            index += 1
        if close < 0:
            body = text[cursor + 1:].rstrip('…').rstrip('.')
            end = len(text)
        else:
            body = text[cursor + 1:close]
            end = close + 1
            if end < len(text) and text[end] == '>':
                end += 1
        replacement = _format_parsed_ue_struct(name, body)
        text = text[:start] + replacement + text[end:]
    return _UE_STRUCT_PTR_RE.sub('', text)


def format_ue_struct(value, depth=0):
    """Expand a UE Python struct via its dump string (no UPROPERTY probing)."""
    del depth
    try:
        return pretty_export_text(str(value))
    except Exception:
        return ''


def _keep_nsloctext_display(match):
    quote = match.group(3)
    return quote + match.group(4) + quote


def _compact_gameplay_attribute_dumps(text):
    """`(AttributeName='Stamina',Attribute=…,AttributeOwner=…)` → `(AttributeName='Stamina')`."""
    marker = '(AttributeName='
    pieces = []
    cursor = 0
    while True:
        start = text.find(marker, cursor)
        if start < 0:
            pieces.append(text[cursor:])
            break
        pieces.append(text[cursor:start])
        name_open = start + len(marker)
        if name_open >= len(text) or text[name_open] not in '\'"':
            pieces.append(text[start:name_open])
            cursor = name_open
            continue
        quote = text[name_open]
        name_close = text.find(quote, name_open + 1)
        if name_close < 0:
            pieces.append(text[start:])
            break
        depth = 0
        close = -1
        for index in range(start, len(text)):
            char = text[index]
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth == 0:
                    close = index
                    break
        if close < 0:
            pieces.append(text[start:])
            break
        pieces.append(marker + text[name_open:name_close + 1] + ')')
        cursor = close + 1
    return ''.join(pieces)


def _shorten_float_token(token):
    """`0.0` → `0`, `0.500000` → `0.5`, `2.000000` → `2`."""
    sign = ''
    body = token
    if body.startswith('-'):
        sign = '-'
        body = body[1:]
    elif body.startswith('+'):
        body = body[1:]
    if '.' not in body:
        return token
    whole, frac = body.split('.', 1)
    whole = whole.lstrip('0') or '0'
    frac = frac.rstrip('0')
    if not frac:
        if whole == '0':
            return '0'
        return sign + whole
    return sign + whole + '.' + frac


def _shorten_content_path(match):
    path = match.group(0)
    last = path.rsplit('/', 1)[-1]
    if '.' in last:
        pkg, obj = last.split('.', 1)
        last = pkg if obj in (pkg, pkg + '_C') else obj
    return '…/' + last


def normalize_node_class(class_name):
    """`K2Node_Event` → `Event` (same rules as Mermaid type lines)."""
    return pretty_export_text(class_name or '').strip()


def node_class_is_root(class_name):
    name = normalize_node_class(class_name)
    if name in ROOT_NODE_CLASSES:
        return True
    return name.startswith('AnimGraphNode_Root')


def node_class_is_anim_pose(class_name):
    """Pose/state-machine anim nodes. Mermaid of these graphs is not useful for LLM."""
    return normalize_node_class(class_name).startswith('AnimGraphNode_')


def node_class_is_call_function(class_name):
    return normalize_node_class(class_name) == 'CallFunction'


def is_anim_pose_graph_name(name):
    return str(name or '').strip().lower() == 'animgraph'


def dump_is_anim_pose_graph(dump):
    nodes = (dump or {}).get('nodes') or []
    if not nodes:
        return False
    return all(node_class_is_anim_pose(node.get('class')) for node in nodes)


def dump_is_trivial(dump):
    """True when the graph is empty, stub-only, a pose AnimGraph, or unwired Event+CallFunction."""
    if dump_is_anim_pose_graph(dump):
        return True
    nodes = (dump or {}).get('nodes') or []
    if not nodes:
        return True
    if all(node_class_is_root(node.get('class')) for node in nodes):
        return True
    edges = (dump or {}).get('edges') or []
    if edges:
        return False
    return all(
        node_class_is_root(node.get('class')) or node_class_is_call_function(node.get('class'))
        for node in nodes
    )


def meaningful_dumps(dumps):
    """Keep graphs that have at least one non-stub / non-pose node."""
    return [dump for dump in (dumps or []) if not dump_is_trivial(dump)]


def mermaid_node_class_from_label(label):
    """Class line is the first extra after the title that is not a `Pin: value` literal."""
    parts = (label or '').split('<br/>')
    if len(parts) < 2:
        return ''
    for part in parts[1:]:
        token = part.strip()
        if not token or ': ' in token:
            continue
        return token
    return ''


def mermaid_block_is_trivial(block):
    """True when one ```mermaid``` body is stub, pose, or unwired Event+CallFunction."""
    labels = _MERMAID_NODE_LABEL_RE.findall(block or '')
    if not labels:
        return True
    classes = [mermaid_node_class_from_label(label) for label in labels]
    if any(not cls for cls in classes):
        return False
    if all(node_class_is_root(cls) or node_class_is_anim_pose(cls) for cls in classes):
        return True
    if _MERMAID_EDGE_RE.search(block or ''):
        return False
    return all(
        node_class_is_root(cls) or node_class_is_anim_pose(cls) or cls == 'CallFunction'
        for cls in classes
    )


def combined_markdown_is_trivial(text):
    """Parse an exported `{Asset}.md` without loading the Blueprint.

    Unknown node labels (no class line) are treated as meaningful so we keep the file.
    Each mermaid fence is judged on its own; the file is trivial only if every fence is.
    """
    if not text or not str(text).strip():
        return True
    blocks = _MERMAID_FENCE_RE.findall(str(text))
    if not blocks:
        return True
    return all(mermaid_block_is_trivial(block) for block in blocks)


def combined_markdown_has_trivial_block(text):
    """True when at least one mermaid fence is skippable (pose/stub) — rewrite even if mixed."""
    blocks = _MERMAID_FENCE_RE.findall(str(text or ''))
    return any(mermaid_block_is_trivial(block) for block in blocks)


def export_asset_stem(name):
    """`/Game/Foo/GA_Bar.GA_Bar_C` / `GA_Bar.md` → `GA_Bar`."""
    text = str(name or '').strip().replace('\\', '/')
    if not text:
        return ''
    text = text.rsplit('/', 1)[-1]
    if text.lower().endswith('.uasset') or text.lower().endswith('.umap'):
        text = text.rsplit('.', 1)[0]
    elif text.lower().endswith('.md'):
        text = text[:-3]
    if '.' in text:
        text = text.rsplit('.', 1)[-1]
    if text.endswith('_C') and len(text) > 2:
        text = text[:-2]
    return text


def is_gas_export_name(name):
    """True for Content assets whose stem is `GA_*` or `GE_*`."""
    return export_asset_stem(name).startswith(GAS_EXPORT_PREFIXES)


def is_data_asset_export_name(name):
    """True for Content assets whose stem is `DA_*`."""
    return export_asset_stem(name).startswith(DATA_ASSET_EXPORT_PREFIXES)


def is_non_graph_export_name(name):
    return is_gas_export_name(name) or is_data_asset_export_name(name)


def class_rows_are_meaningful(rows):
    for row in rows or []:
        if str(row.get('name') or '').strip() and str(row.get('value') or '').strip():
            return True
    return False


def keep_gas_class_only_export(asset_name, class_settings=None, class_defaults=None):
    """GA_/GE_ keep a markdown file when Class Settings/Defaults have values, even with no graphs."""
    return is_gas_export_name(asset_name) and (
        class_rows_are_meaningful(class_settings)
        or class_rows_are_meaningful(class_defaults)
    )


def keep_class_only_export(asset_name, class_settings=None, class_defaults=None,
                           components=None):
    """Keep `{Asset}.md` when Class Defaults / components exist (not mermaid-trivial skip)."""
    if keep_gas_class_only_export(asset_name, class_settings, class_defaults):
        return True
    if class_rows_are_meaningful(class_defaults):
        return True
    if components:
        return True
    return False


def markdown_has_class_content(text):
    """True when a Class Settings/Defaults list row is present (not `_(none)_`)."""
    if not text:
        return False
    return bool(re.search(r'^- \*\*[^*]+?\*\*', str(text), re.M))


def existing_gas_export_is_keepable(md_path, text=None):
    if not is_gas_export_name(os.path.basename(md_path or '')):
        return False
    body = text
    if body is None:
        if not md_path or not os.path.isfile(md_path):
            return False
        try:
            with open(md_path, encoding='utf-8') as handle:
                body = handle.read()
        except Exception:
            return False
    return markdown_has_class_content(body)


def markdown_has_json_content(text):
    """True when a ```json fence has a non-empty object/array."""
    for block in _JSON_FENCE_RE.findall(str(text or '')):
        stripped = block.strip()
        if stripped and stripped not in ('{}', '[]', 'null'):
            return True
    return False


def existing_da_export_is_keepable(md_path, text=None):
    if not is_data_asset_export_name(os.path.basename(md_path or '')):
        return False
    body = text
    if body is None:
        if not md_path or not os.path.isfile(md_path):
            return False
        try:
            with open(md_path, encoding='utf-8') as handle:
                body = handle.read()
        except Exception:
            return False
    return markdown_has_json_content(body)


def existing_class_export_is_keepable(md_path, text=None):
    body = text
    if body is None:
        if not md_path or not os.path.isfile(md_path):
            return False
        try:
            with open(md_path, encoding='utf-8') as handle:
                body = handle.read()
        except Exception:
            return False
    return markdown_has_class_content(body)


def existing_non_graph_export_is_keepable(md_path, text=None):
    return (
        existing_gas_export_is_keepable(md_path, text)
        or existing_da_export_is_keepable(md_path, text)
        or existing_class_export_is_keepable(md_path, text)
    )


def _json_number(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    text = _shorten_float_token(str(value))
    if '.' in text:
        return float(text)
    return int(text)


def _json_key(name):
    return display_member_variable_name(name) or str(name or '')


def _is_uobject_like(value):
    return callable(getattr(value, 'get_path_name', None)) or callable(
        getattr(value, 'get_editor_property', None))


def _uobject_is_inner(value, parent):
    if parent is None:
        return False
    get_outer = getattr(value, 'get_outer', None)
    if not callable(get_outer):
        return False
    try:
        outer = get_outer()
    except Exception:
        return False
    if outer is parent:
        return True
    try:
        return outer == parent
    except Exception:
        return False


def ue_value_to_jsonable(value, depth=0, parent=None):
    """Convert a UE/Python value to JSON-friendly data. `_JSON_OMIT` means skip the key."""
    if value is _JSON_OMIT or value is None or depth > 5:
        return _JSON_OMIT
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return _json_number(value)
    if isinstance(value, (bytes, bytearray)):
        return _JSON_OMIT
    if isinstance(value, str):
        if _looks_like_ue_struct(value) or "<Struct '" in value:
            tags = collect_gameplay_tag_names(value)
            if tags:
                name = ''
                match = _UE_STRUCT_REPR_RE.search(value)
                if match:
                    name = match.group('name')
                if 'TagContainer' in name or len(tags) > 1:
                    return tags
                return tags[0]
            text = pretty_export_text(value)
            return text if text else _JSON_OMIT
        text = pretty_export_text(value)
        return text if text else _JSON_OMIT
    if _looks_like_ue_struct(value):
        return ue_value_to_jsonable(str(value), depth=depth, parent=parent)
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:32]:
            converted = ue_value_to_jsonable(item, depth=depth + 1, parent=parent)
            if converted is _JSON_OMIT:
                continue
            label = pretty_export_text(str(key)) or str(key)
            out[label] = converted
        return out if out else _JSON_OMIT
    if isinstance(value, (list, tuple, set, frozenset)):
        parts = []
        for item in list(value)[:32]:
            converted = ue_value_to_jsonable(item, depth=depth + 1, parent=parent)
            if converted is not _JSON_OMIT:
                parts.append(converted)
        return parts
    if unreal is not None:
        array_cls = getattr(unreal, 'Array', None)
        if array_cls is not None:
            try:
                if isinstance(value, array_cls):
                    return ue_value_to_jsonable(_as_list(value), depth=depth, parent=parent)
            except Exception:
                pass
        map_cls = getattr(unreal, 'Map', None)
        if map_cls is not None:
            try:
                if isinstance(value, map_cls):
                    mapped = {}
                    for key in value:
                        mapped[key] = value[key]
                    return ue_value_to_jsonable(mapped, depth=depth, parent=parent)
            except Exception:
                pass
    if _looks_like_gas_subobject(value) or (
            _is_uobject_like(value) and _uobject_is_inner(value, parent)):
        nested = collect_data_asset_json(value, depth=depth + 1, parent=value)
        type_name = pretty_export_text(_gas_type_name(value))
        if nested:
            if type_name:
                nested = dict(nested)
                nested['_Class'] = type_name
            return nested
        if type_name:
            return type_name
        return _JSON_OMIT
    if _is_uobject_like(value):
        get_path = getattr(value, 'get_path_name', None)
        if callable(get_path):
            try:
                text = pretty_export_text(get_path())
                return text if text else _JSON_OMIT
            except Exception:
                pass
        text = pretty_export_text(_gas_type_name(value))
        return text if text else _JSON_OMIT
    text = pretty_export_text(str(value))
    if text in ('None', 'none', 'null', ''):
        return _JSON_OMIT
    return text


def skip_object_prop_name(name, skip_engine_component=False):
    snake = _to_snake_property_name(name)
    if snake in DATA_ASSET_SKIP_NAMES:
        return True
    if not skip_engine_component:
        return False
    if snake in COMPONENT_SKIP_NAMES:
        return True
    for prefix in COMPONENT_SKIP_PREFIXES:
        if snake.startswith(prefix):
            return True
    return False


def _wrapper_property_names(obj):
    """Names from instance/class dicts. Never dir() a live UObject."""
    names = []
    seen = set()

    def add(name):
        if not name or name.startswith('_') or name in seen:
            return
        seen.add(name)
        names.append(name)

    try:
        for name in vars(obj):
            add(name)
    except TypeError:
        pass
    try:
        for cls in type(obj).__mro__:
            if cls is object:
                continue
            try:
                mapping = vars(cls)
            except TypeError:
                continue
            for name, attr in mapping.items():
                if callable(attr) and not isinstance(attr, property):
                    continue
                add(name)
    except Exception:
        pass
    return names


def list_data_asset_property_names(obj, skip_engine_component=False):
    """Property names for a DataAsset / component dump. Not the Blueprint CDO hot path."""
    names = []
    seen = set()
    if skip_engine_component:
        raw = _wrapper_property_names(obj)
    else:
        try:
            raw = dir(obj)
        except Exception:
            return names
    for name in raw:
        if not name or name.startswith('_'):
            continue
        if not keep_class_default_member(name):
            continue
        if skip_object_prop_name(name, skip_engine_component=skip_engine_component):
            continue
        type_attr = getattr(type(obj), name, None)
        if callable(type_attr) and not isinstance(type_attr, property):
            continue
        key = (_to_snake_property_name(name) or name).lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def collect_data_asset_json(obj, names=None, depth=0, parent=None,
                            skip_engine_component=False):
    """JSON-friendly dict of DataAsset / inner-object editor properties."""
    if obj is None or depth > 5:
        return {}
    listed = names if names is not None else list_data_asset_property_names(
        obj, skip_engine_component=skip_engine_component)
    out = {}
    owner = parent if parent is not None else obj
    for name in listed:
        if len(out) >= CLASS_DEFAULTS_MAX_ROWS:
            break
        if not keep_class_default_member(name):
            continue
        if skip_object_prop_name(name, skip_engine_component=skip_engine_component):
            continue
        found, value = read_cdo_property(obj, name)
        if not found:
            continue
        converted = ue_value_to_jsonable(value, depth=depth + 1, parent=owner)
        if converted is _JSON_OMIT:
            continue
        label = _json_key(name)
        if not label:
            continue
        out[label] = converted
    return out


def assemble_data_asset_markdown(stem, data):
    """`# DA_Foo` plus a json fence. `data` is already JSON-friendly."""
    payload = json.dumps(data if data is not None else {}, indent=2, ensure_ascii=False)
    return '# {}\n\n```json\n{}\n```\n'.format(stem or 'DA', payload)


def write_data_asset_export_file(stem, data, out_dir, filename=None):
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    if not filename:
        filename = export_markdown_filename(stem)
    md_path = os.path.join(out_dir, filename)
    body = assemble_data_asset_markdown(stem, data)
    with open(md_path, 'w', encoding='utf-8') as handle:
        handle.write(body)
        if body and not body.endswith('\n'):
            handle.write('\n')
    return md_path


def mermaid_escape(text):
    if text is None:
        return ''
    text = str(text).replace('\r\n', '\n').replace('\r', '\n')
    text = text.replace('"', "'").replace('[', '(').replace(']', ')')
    text = text.replace('\n', '<br/>')
    return text


def _label_text(text):
    return mermaid_escape(pretty_export_text(text))


def dump_to_mermaid(dump):
    """Return a Mermaid flowchart from a graph dump dict."""
    bp = mermaid_escape(dump.get('blueprint') or 'Blueprint')
    graph = mermaid_escape(dump.get('graph') or 'Graph')
    lines = [
        'flowchart LR',
        '  %% {} / {}'.format(bp, graph),
    ]
    raw_count = dump.get('raw_node_count')
    if raw_count is not None:
        lines.append('  %% raw_node_count={}'.format(raw_count))
    for warning in dump.get('warnings') or []:
        lines.append('  %% warning: {}'.format(mermaid_escape(warning)))
    node_ids = _assign_short_ids(dump.get('nodes') or [], 'n')
    comment_ids = _assign_short_ids(dump.get('comments') or [], 'c')
    for node in dump.get('nodes') or []:
        nid = _short_id(node_ids, node.get('id'), 'n')
        title = _label_text(node.get('title') or node.get('id'))
        extra = []
        class_name = _label_text(node.get('class') or '')
        if class_name and class_name != title:
            extra.append(class_name)
        literals = node.get('literals') or {}
        for pin_name, value in literals.items():
            extra.append('{}: {}'.format(_label_text(pin_name), _label_text(value)))
        label = title if not extra else '{}<br/>{}'.format(title, '<br/>'.join(extra))
        lines.append('  {}["{}"]'.format(nid, label))
    for comment in dump.get('comments') or []:
        title = _label_text(comment.get('title') or 'Comment')
        member_ids = comment.get('member_ids') or []
        if not member_ids:
            lines.append('  %% comment: {}'.format(title))
            continue
        lines.append('  subgraph {} ["{}"]'.format(
            _short_id(comment_ids, comment.get('id'), 'c'), title))
        lines.append('    direction LR')
        for member_id in member_ids:
            mapped = node_ids.get(_dump_id_key(member_id))
            if mapped:
                lines.append('    {}'.format(mapped))
        lines.append('  end')
    for edge in dump.get('edges') or []:
        src = _short_id(node_ids, edge.get('from'), 'n')
        dst = _short_id(node_ids, edge.get('to'), 'n')
        kind = edge.get('kind') or 'data'
        arrow = '-->' if kind == 'exec' else '-.->'
        pin_from = _label_text(edge.get('from_pin') or '')
        pin_to = _label_text(edge.get('to_pin') or '')
        label = pin_from
        if pin_to and pin_to not in EXEC_PIN_NAMES:
            label = '{} -> {}'.format(pin_from, pin_to) if pin_from else pin_to
        if label:
            lines.append('  {} {}|"{}"| {}'.format(src, arrow, label, dst))
        else:
            lines.append('  {} {} {}'.format(src, arrow, dst))
    return '\n'.join(lines) + '\n'


def write_export_files(dump, out_dir, stamp=None):
    """Write a Mermaid markdown file. Returns md_path."""
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    md_path = os.path.join(out_dir, export_markdown_filename(
        dump.get('blueprint'), dump.get('graph'), stamp=stamp))
    mermaid = dump_to_mermaid(dump)
    with open(md_path, 'w', encoding='utf-8') as handle:
        handle.write('# {} / {}\n\n'.format(dump.get('blueprint') or '', dump.get('graph') or ''))
        handle.write('```mermaid\n')
        handle.write(mermaid)
        if not mermaid.endswith('\n'):
            handle.write('\n')
        handle.write('```\n')
    return md_path


def ordered_graph_names(names):
    """EventGraph first (if present), then the rest A–Z (case-insensitive)."""
    unique = []
    seen = set()
    for name in names or []:
        text = str(name)
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    event = [name for name in unique if name.lower() == 'eventgraph']
    rest = sorted(
        (name for name in unique if name.lower() != 'eventgraph'),
        key=lambda name: name.lower(),
    )
    return event + rest


def dumps_to_combined_markdown(dumps):
    """Join ordered graph dumps into one markdown file (H1 per graph)."""
    parts = []
    for dump in dumps or []:
        heading = str(dump.get('graph') or 'Graph')
        mermaid = dump_to_mermaid(dump)
        if not mermaid.endswith('\n'):
            mermaid += '\n'
        parts.append('# {}\n\n```mermaid\n{}```\n'.format(heading, mermaid))
    return '\n'.join(parts)


def _gas_type_name(value):
    get_class = getattr(value, 'get_class', None)
    if callable(get_class):
        try:
            cls = get_class()
            name_fn = getattr(cls, 'get_name', None)
            if callable(name_fn):
                text = str(name_fn() or '')
                if text:
                    return text
        except Exception:
            pass
    get_name = getattr(value, 'get_name', None)
    if callable(get_name):
        try:
            text = str(get_name() or '')
            if text:
                return text
        except Exception:
            pass
    return type(value).__name__


def _looks_like_gas_subobject(value):
    if value is None or isinstance(value, (str, bytes, bytearray, bool, int, float)):
        return False
    if not callable(getattr(value, 'get_editor_property', None)):
        return False
    name = _gas_type_name(value)
    return 'GameplayEffectComponent' in name or 'GameplayModifierInfo' in name


def format_gas_subobject(value, depth=0):
    """Expand a GE component / modifier instead of dumping its UObject path."""
    type_name = pretty_export_text(_gas_type_name(value))
    if depth >= 4:
        return type_name
    parts = []
    seen = set()
    for base in GAS_SUBOBJECT_PROP_BASES:
        key = member_variable_basename(base).lower()
        if key in seen:
            continue
        found, current = read_cdo_property(value, base)
        if not found:
            continue
        seen.add(key)
        text = format_export_value(current, depth + 1)
        if not text:
            continue
        parts.append('{}={}'.format(display_member_variable_name(base), text))
    if not parts:
        return type_name
    return '{}({})'.format(type_name, ', '.join(parts))


def format_export_value(value, depth=0):
    """LLM-facing string for a Class Settings / Class Defaults value."""
    if value is None or depth > 4:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return pretty_export_text(str(value))
    if isinstance(value, (bytes, bytearray)):
        return ''
    if isinstance(value, str):
        return pretty_export_text(value)
    if _looks_like_ue_struct(value):
        return pretty_export_text(str(value))
    if _looks_like_gas_subobject(value):
        return format_gas_subobject(value, depth)
    to_string = getattr(value, 'to_string', None)
    if callable(to_string):
        try:
            text = to_string()
            if text and not str(text).lstrip().startswith('<Struct '):
                return pretty_export_text(text)
        except Exception:
            pass
    get_path = getattr(value, 'get_path_name', None)
    if callable(get_path):
        try:
            return pretty_export_text(get_path())
        except Exception:
            pass
    get_name = getattr(value, 'get_name', None)
    if callable(get_name) and not isinstance(value, type):
        try:
            text = get_name()
            if text and not str(text).lstrip().startswith('<Struct '):
                return pretty_export_text(text)
        except Exception:
            pass
    if isinstance(value, dict):
        text = pretty_export_text(str(value))
        return text[:237] + '…' if len(text) > 240 else text
    if isinstance(value, (list, tuple, set, frozenset)):
        parts = [format_export_value(item, depth + 1) for item in list(value)[:32]]
        return ', '.join(part for part in parts if part)
    if unreal is not None:
        array_cls = getattr(unreal, 'Array', None)
        if array_cls is not None:
            try:
                if isinstance(value, array_cls):
                    parts = [
                        format_export_value(item, depth + 1)
                        for item in _as_list(value)[:32]
                    ]
                    return ', '.join(part for part in parts if part)
            except Exception:
                pass
    text = pretty_export_text(str(value))
    if text in ('None', 'none', 'null'):
        return ''
    return text


def format_class_section_markdown(title, rows):
    lines = ['# {}'.format(title), '']
    if not rows:
        lines.append('_(none)_')
        lines.append('')
        return '\n'.join(lines)
    for row in rows:
        name = pretty_export_text(row.get('name') or '')
        value = pretty_export_text(row.get('value') or '')
        if not name:
            continue
        if value:
            lines.append('- **{}**: {}'.format(name, value))
        else:
            lines.append('- **{}**'.format(name))
    if len(lines) == 2:
        lines.append('_(none)_')
    lines.append('')
    return '\n'.join(lines)


def _component_prop_cell(value):
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return pretty_export_text(str(value))
    if isinstance(value, str):
        return pretty_export_text(value)
    if isinstance(value, list):
        parts = [_component_prop_cell(item) for item in value]
        return ', '.join(part for part in parts if part)
    if isinstance(value, dict):
        return pretty_export_text(json.dumps(value, ensure_ascii=False))
    return pretty_export_text(str(value))


def format_components_markdown(entries):
    """`# Components` with one `## Name (Class)` block per component that has properties."""
    if not entries:
        return ''
    lines = ['# Components', '']
    for entry in entries:
        name = pretty_export_text(entry.get('name') or 'Component')
        cls = pretty_export_text(entry.get('class') or '')
        if cls and cls not in (name,):
            lines.append('## {} ({})'.format(name, cls))
        else:
            lines.append('## {}'.format(name))
        lines.append('')
        props = entry.get('props') or {}
        if not props:
            lines.append('_(none)_')
            lines.append('')
            continue
        for key, value in props.items():
            label = pretty_export_text(str(key))
            if not label:
                continue
            cell = _component_prop_cell(value)
            if cell:
                lines.append('- **{}**: {}'.format(label, cell))
            else:
                lines.append('- **{}**'.format(label))
        lines.append('')
    return '\n'.join(lines)


def assemble_blueprint_markdown(dumps, class_settings=None, class_defaults=None,
                                components=None):
    """Class Settings + Class Defaults + Components, then graph mermaid."""
    header = (
        format_class_section_markdown('Class Settings', class_settings or [])
        + '\n'
        + format_class_section_markdown('Class Defaults', class_defaults or [])
    )
    component_block = format_components_markdown(components or [])
    if component_block:
        header = header + '\n' + component_block
    body = dumps_to_combined_markdown(dumps)
    if body:
        return header + '\n' + body
    return header


def write_combined_export_file(dumps, out_dir, stamp=None, filename=None,
                               class_settings=None, class_defaults=None,
                               components=None):
    """Write `{Blueprint}.md` with class header + every graph. Returns md_path."""
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    if not filename:
        blueprint = 'BP'
        if dumps:
            blueprint = dumps[0].get('blueprint') or blueprint
        filename = export_markdown_filename(blueprint, stamp=stamp)
    md_path = os.path.join(out_dir, filename)
    body = assemble_blueprint_markdown(
        dumps, class_settings=class_settings, class_defaults=class_defaults,
        components=components)
    with open(md_path, 'w', encoding='utf-8') as handle:
        handle.write(body)
        if body and not body.endswith('\n'):
            handle.write('\n')
    return md_path


def _dump_id_key(raw):
    if isinstance(raw, dict):
        raw = raw.get('id')
    return str(raw or 'x')


def _assign_short_ids(items, prefix):
    """Map dump ids to compact Mermaid ids: n0, n1, … / c0, c1, …"""
    mapping = {}
    for item in items or []:
        key = _dump_id_key(item)
        if key not in mapping:
            mapping[key] = '{}{}'.format(prefix, len(mapping))
    return mapping


def _short_id(mapping, raw, prefix):
    key = _dump_id_key(raw)
    found = mapping.get(key)
    if found:
        return found
    return _mermaid_id(prefix, raw)


def _mermaid_id(prefix, raw):
    token = re.sub(r'[^A-Za-z0-9_]', '_', str(raw or 'x'))
    if not token or token[0].isdigit():
        token = 'x' + token
    return prefix + '_' + token


def _safe_filename(name):
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '_', str(name or 'x'))
    return cleaned[:80] or 'x'


def export_stamp(when=None):
    """`YYYY-MM-DD-HH-mm` local time (no seconds)."""
    if when is None:
        return time.strftime('%Y-%m-%d-%H-%M')
    return time.strftime('%Y-%m-%d-%H-%M', when)


def export_markdown_filename(blueprint, graph=None, stamp=None):
    """Combined: `{BP}.md`. One graph: `{BP} - {Graph}.md`. `stamp` ignored (compat)."""
    bp = _safe_filename(blueprint or 'BP')
    if graph:
        return '{} - {}.md'.format(bp, _safe_filename(graph))
    return '{}.md'.format(bp)


def _mtime_seconds(value):
    return int(round(float(value)))


def should_refresh_export(md_path, source_mtime):
    """True when markdown is missing or its mtime does not match the Blueprint on disk."""
    if source_mtime is None or not md_path or not os.path.isfile(md_path):
        return True
    return _mtime_seconds(os.path.getmtime(md_path)) != _mtime_seconds(source_mtime)


def stamp_export_mtime(md_path, source_mtime):
    """Set `.md` atime/mtime to the Blueprint's on-disk mtime so the next scan can skip."""
    if source_mtime is None or not md_path or not os.path.isfile(md_path):
        return False
    os.utime(md_path, (source_mtime, source_mtime))
    return True


def trivial_skip_path(md_path):
    if not md_path:
        return None
    return md_path + TRIVIAL_SKIP_SUFFIX


def trivial_skip_is_current(md_path, source_mtime):
    """True when a gitignored skip marker matches the Blueprint mtime (do not reload)."""
    skip_path = trivial_skip_path(md_path)
    if source_mtime is None or not skip_path or not os.path.isfile(skip_path):
        return False
    if _mtime_seconds(os.path.getmtime(skip_path)) != _mtime_seconds(source_mtime):
        return False
    try:
        with open(skip_path, encoding='utf-8') as handle:
            lines = [line.strip() for line in handle.read().splitlines() if line.strip()]
    except Exception:
        return False
    return len(lines) >= 2 and lines[1] == TRIVIAL_SKIP_GENERATION


def stamp_trivial_skip(md_path, source_mtime):
    """Write `{Asset}.md.skip` so Export Project can skip root-only Blueprints without loading."""
    skip_path = trivial_skip_path(md_path)
    if not skip_path:
        return None
    folder = os.path.dirname(skip_path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    with open(skip_path, 'w', encoding='utf-8') as handle:
        handle.write('root-only\n{}\n'.format(TRIVIAL_SKIP_GENERATION))
    stamp_export_mtime(skip_path, source_mtime)
    return skip_path


def clear_trivial_skip(md_path):
    skip_path = trivial_skip_path(md_path)
    if not skip_path or not os.path.isfile(skip_path):
        return False
    try:
        os.remove(skip_path)
    except OSError:
        return False
    return True


def existing_export_is_trivial(md_path):
    if not md_path or not os.path.isfile(md_path):
        return False
    try:
        with open(md_path, encoding='utf-8') as handle:
            return combined_markdown_is_trivial(handle.read())
    except Exception:
        return False


def existing_export_has_trivial_block(md_path):
    if not md_path or not os.path.isfile(md_path):
        return False
    try:
        with open(md_path, encoding='utf-8') as handle:
            return combined_markdown_has_trivial_block(handle.read())
    except Exception:
        return False


def delete_export_markdown(md_path, root=None):
    """Delete a leftover `{Asset}.md` and prune empty parent dirs up to `root`."""
    if not md_path or not os.path.isfile(md_path):
        return False
    folder = os.path.dirname(md_path)
    try:
        os.remove(md_path)
    except OSError:
        return False
    _prune_empty_export_dirs(folder, root)
    return True


def _prune_empty_export_dirs(folder, root):
    if not folder or not root:
        return
    root_abs = os.path.normpath(os.path.abspath(root))
    current = os.path.normpath(os.path.abspath(folder))
    while True:
        if os.path.normcase(current) == os.path.normcase(root_abs):
            return
        try:
            common = os.path.commonpath([current, root_abs])
        except ValueError:
            return
        if os.path.normcase(common) != os.path.normcase(root_abs):
            return
        try:
            if os.listdir(current):
                return
            os.rmdir(current)
        except OSError:
            return
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def record_trivial_export(md_path, source_mtime, root=None):
    """Drop leftover mermaid and stamp a skip marker. `root` unused (compat)."""
    deleted = False
    if md_path and os.path.isfile(md_path):
        try:
            os.remove(md_path)
            deleted = True
        except OSError:
            deleted = False
    stamp_trivial_skip(md_path, source_mtime)
    return deleted


def strip_legacy_yaml_header(md_path, source_mtime=None):
    """Remove a leftover `---` YAML block from an earlier git-frontmatter experiment."""
    if not md_path or not os.path.isfile(md_path):
        return False
    with open(md_path, encoding='utf-8') as handle:
        text = handle.read()
    match = _LEGACY_YAML_HEADER_RE.match(text)
    if not match:
        return False
    body = text[match.end():].lstrip('\n')
    with open(md_path, 'w', encoding='utf-8') as handle:
        handle.write(body)
        if body and not body.endswith('\n'):
            handle.write('\n')
    stamp_export_mtime(md_path, source_mtime)
    return True


def project_content_dir(content_dir=None):
    if content_dir:
        return os.path.normpath(content_dir)
    if unreal is not None:
        return os.path.normpath(str(unreal.Paths.project_content_dir()))
    return os.path.normpath(os.path.join(os.getcwd(), 'Content'))


def _package_path_without_object(package_path):
    text = str(package_path or '').replace('\\', '/')
    leaf = text.rsplit('/', 1)[-1]
    if '.' in leaf:
        text = text.rsplit('.', 1)[0]
    return text


def package_disk_path(package_path, content_dir=None):
    """Map `/Game/Foo/Bar` to `{Content}/Foo/Bar.uasset` (or `.umap` if that exists)."""
    text = _package_path_without_object(package_path)
    if text.startswith('/Game/'):
        rel = text[len('/Game/'):]
    elif text.startswith('/Game'):
        rel = text[len('/Game'):].lstrip('/')
    elif text.startswith('Game/'):
        rel = text[len('Game/'):]
    else:
        return None
    parts = [part for part in rel.split('/') if part and part != '..']
    if not parts:
        return None
    base = os.path.join(project_content_dir(content_dir), *parts)
    uasset = base + '.uasset'
    umap = base + '.umap'
    if os.path.isfile(umap) and not os.path.isfile(uasset):
        return umap
    return uasset


def package_source_mtime(package_path, content_dir=None):
    disk = package_disk_path(package_path, content_dir=content_dir)
    if disk and os.path.isfile(disk):
        return os.path.getmtime(disk)
    return None


def export_md_path(package_path, out_dir=None):
    return os.path.join(out_dir or default_output_dir(), project_export_relpath(package_path))


def project_display_name():
    if unreal is None:
        return 'Project'
    getter = getattr(unreal.Paths, 'get_project_file_path', None)
    if getter:
        try:
            base = os.path.splitext(os.path.basename(str(getter())))[0]
            if base:
                return base
        except Exception:
            pass
    return 'Project'


def _package_path_from_blueprint(blueprint):
    for getter in ('get_package', 'get_outermost'):
        method = getattr(blueprint, getter, None)
        if not method:
            continue
        try:
            pkg = method()
            name = getattr(pkg, 'get_name', None)
            if name:
                text = str(name())
                if text:
                    return _package_path_without_object(text)
        except Exception:
            pass
    try:
        return _package_path_without_object(blueprint.get_path_name())
    except Exception:
        return ''


def project_export_relpath(package_path):
    """Map `/Game/Foo/Bar` (or `/Game/Foo/Bar.Bar`) to `Foo/Bar.md`."""
    text = str(package_path or '').replace('\\', '/')
    leaf = text.rsplit('/', 1)[-1]
    if '.' in leaf:
        text = text.rsplit('.', 1)[0]
    if text.startswith('/Game/'):
        text = text[len('/Game/'):]
    elif text.startswith('/Game'):
        text = text[len('/Game'):].lstrip('/')
    elif text.startswith('Game/'):
        text = text[len('Game/'):]
    parts = [_safe_filename(part) for part in text.split('/') if part and part != '..']
    if not parts:
        parts = ['Blueprint']
    filename = parts[-1] + '.md'
    folder = parts[:-1]
    if folder:
        return os.path.join(*(folder + [filename]))
    return filename


# --- Unreal-facing API (import-safe when `unreal` is missing) -----------------

def default_output_dir():
    """`{Project}/Source/BPMermaids` — git-tracked mirror, not Saved."""
    if unreal is not None:
        project = str(unreal.Paths.project_dir())
        return os.path.normpath(os.path.join(project, EXPORT_SUBDIR))
    return os.path.normpath(os.path.join(os.getcwd(), EXPORT_SUBDIR))


def auto_export_state_path(out_dir=None):
    return os.path.join(out_dir or default_output_dir(), AUTO_EXPORT_STATE_FILENAME)


def is_auto_export_enabled(state_path=None):
    """Default off when the state file is missing or unreadable."""
    path = state_path or auto_export_state_path()
    try:
        with open(path, encoding='utf-8') as handle:
            data = json.load(handle)
        return bool(data.get('enabled'))
    except Exception:
        return False


def set_auto_export_enabled(enabled, state_path=None):
    path = state_path or auto_export_state_path()
    folder = os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump({'enabled': bool(enabled)}, handle)
        handle.write('\n')
    if unreal is not None and state_path is None:
        _sync_save_watch()
    return bool(enabled)


def toggle_auto_export(state_path=None):
    enabled = not is_auto_export_enabled(state_path=state_path)
    return set_auto_export_enabled(enabled, state_path=state_path)


def spike_api():
    """Return a dict of live 5.8 API probes. Call from the editor Python console."""
    report = {'unreal': unreal is not None}
    if unreal is None:
        return report
    report['BlueprintEditorLibrary'] = hasattr(unreal, 'BlueprintEditorLibrary')
    lib = getattr(unreal, 'BlueprintEditorLibrary', None)
    for name in (
        'list_graphs', 'list_graph_names', 'find_graph', 'get_node_pos',
        'get_node_size', 'get_node_title', 'list_all_pins', 'get_comment_text',
        'get_nodes_in_comment',
    ):
        report[name] = bool(lib) and hasattr(lib, name)
    editor_cls = getattr(unreal, 'BlueprintGraphEditor', None)
    report['BlueprintGraphEditor'] = editor_cls is not None
    for name in (
        'get_graph_editor', 'get_graph_editor_by_name',
        'list_all_nodes', 'list_comment_nodes',
    ):
        report['BlueprintGraphEditor.' + name] = bool(editor_cls) and hasattr(editor_cls, name)
    report['BlueprintGraphPin'] = hasattr(unreal, 'BlueprintGraphPin')
    report['BlueprintEditorToolMenuContext'] = hasattr(unreal, 'BlueprintEditorToolMenuContext')
    report['ToolMenuSectionDynamic'] = hasattr(unreal, 'ToolMenuSectionDynamic')
    menus = unreal.ToolMenus.get()
    report['toolbar_menu'] = bool(menus.find_menu('AssetEditor.BlueprintEditor.ToolBar'))
    k2 = getattr(unreal, 'K2Node', None)
    report['K2Node.list_all_pins'] = bool(k2) and hasattr(k2, 'list_all_pins')
    report['module_file'] = __file__
    return report


def export_graph_by_path(blueprint_path, graph_name, out_dir=None, notify=True):
    """Load a Blueprint by path and export one named graph."""
    if unreal is None:
        raise RuntimeError('export_graph_by_path requires the Unreal editor')
    blueprint = _load_blueprint(blueprint_path)
    if blueprint is None:
        _log_error('Export Graph: failed to load Blueprint {}'.format(blueprint_path))
        return None
    return export_graph(blueprint, graph_name, out_dir=out_dir, notify=notify)


def export_all_by_path(blueprint_path, out_dir=None, notify=True):
    """Load a Blueprint by path and export every graph into one markdown file."""
    if unreal is None:
        raise RuntimeError('export_all_by_path requires the Unreal editor')
    blueprint = _load_blueprint(blueprint_path)
    if blueprint is None:
        _log_error('Export All: failed to load Blueprint {}'.format(blueprint_path))
        return None
    return export_all(blueprint, out_dir=out_dir, notify=notify)


class _AssetRef(object):
    """Minimal AssetData stand-in for Content-walked GA_/GE_ packages."""

    __slots__ = ('package_name', 'asset_name')

    def __init__(self, package_name, asset_name):
        self.package_name = package_name
        self.asset_name = asset_name


def _asset_package_name(asset):
    package = getattr(asset, 'package_name', None)
    if package:
        return str(package)
    return ''


def _is_exportable_game_blueprint(asset):
    helpers = getattr(unreal, 'AssetRegistryHelpers', None)
    if helpers is not None and hasattr(helpers, 'is_redirector'):
        try:
            if helpers.is_redirector(asset):
                return False
        except Exception:
            pass
    package = _asset_package_name(asset)
    if not package.startswith('/Game/'):
        return False
    name = str(getattr(asset, 'asset_name', '') or package.rsplit('/', 1)[-1])
    if name.startswith(('SKEL_', 'REINST_', 'TRASH_', 'DEAD_')):
        return False
    return True


def iter_content_prefixed_packages(prefixes, content_dir=None):
    """`(package, stem)` for `Prefix*.uasset` under Content."""
    prefixes = tuple(prefixes or ())
    root = project_content_dir(content_dir)
    found = []
    if not prefixes or not os.path.isdir(root):
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name for name in dirnames
            if name.lower() not in _SKIP_CONTENT_WALK_DIRS
        ]
        for filename in filenames:
            lower = filename.lower()
            if not lower.endswith('.uasset'):
                continue
            stem = filename[:-7]
            if not export_asset_stem(stem).startswith(prefixes):
                continue
            rel = os.path.relpath(os.path.join(dirpath, filename), root)
            rel = rel.replace('\\', '/')
            if rel.endswith('.uasset'):
                rel = rel[:-7]
            found.append(('/Game/' + rel, stem))
    found.sort(key=lambda item: item[0].lower())
    return found


def iter_content_gas_packages(content_dir=None):
    """`(package, stem)` for every `GA_*.uasset` / `GE_*.uasset` under Content."""
    return iter_content_prefixed_packages(GAS_EXPORT_PREFIXES, content_dir)


def iter_content_da_packages(content_dir=None):
    """`(package, stem)` for every `DA_*.uasset` under Content."""
    return iter_content_prefixed_packages(DATA_ASSET_EXPORT_PREFIXES, content_dir)


def _list_assets_by_class(registry, script, class_name):
    if registry is None or unreal is None:
        return []
    try:
        class_path = unreal.TopLevelAssetPath(script, class_name)
        try:
            assets = registry.get_assets_by_class(class_path, True)
        except TypeError:
            assets = registry.get_assets_by_class(class_path)
    except Exception:
        return []
    return _as_list(assets)


def _list_game_blueprint_assets():
    found = []
    seen = set()

    def _add(asset):
        if not _is_exportable_game_blueprint(asset):
            return
        package = _asset_package_name(asset)
        key = package.lower()
        if not key or key in seen:
            return
        seen.add(key)
        found.append(asset)

    helpers = getattr(unreal, 'AssetRegistryHelpers', None) if unreal is not None else None
    registry = None
    if helpers is not None and hasattr(helpers, 'get_asset_registry'):
        try:
            registry = helpers.get_asset_registry()
        except Exception as error:
            _log_error('Export Project: AssetRegistry failed: {}'.format(error))
            registry = None
    if registry is not None:
        for script, class_name in _EXPORT_ASSET_CLASSES:
            for asset in _list_assets_by_class(registry, script, class_name):
                _add(asset)
    for package, stem in iter_content_gas_packages():
        if package.lower() in seen:
            continue
        _add(_AssetRef(package, stem))
    for package, stem in iter_content_da_packages():
        if package.lower() in seen:
            continue
        _add(_AssetRef(package, stem))
    found.sort(key=lambda item: _asset_package_name(item).lower())
    return found


def export_project(out_dir=None, notify=True):
    """Export All for every `/Game` Blueprint into the Content-mirrored tree."""
    if unreal is None:
        raise RuntimeError('export_project requires the Unreal editor')
    root = out_dir or default_output_dir()
    if not os.path.isdir(root):
        os.makedirs(root)
    assets = _list_game_blueprint_assets()
    wrote = []
    skipped = 0
    skipped_uptodate = 0
    skipped_trivial = 0
    failed = []
    cancelled = False
    total = len(assets)
    project_name = project_display_name()

    def _one(asset):
        package = _asset_package_name(asset)
        md_path = export_md_path(package, root)
        source_mtime = package_source_mtime(package)
        if not should_refresh_export(md_path, source_mtime):
            strip_legacy_yaml_header(md_path, source_mtime)
            if existing_export_is_trivial(md_path):
                if existing_non_graph_export_is_keepable(md_path):
                    clear_trivial_skip(md_path)
                    return 'uptodate'
                record_trivial_export(md_path, source_mtime, root)
                return 'trivial'
            if not existing_export_has_trivial_block(md_path):
                clear_trivial_skip(md_path)
                return 'uptodate'
        name = str(getattr(asset, 'asset_name', '') or package.rsplit('/', 1)[-1])
        if trivial_skip_is_current(md_path, source_mtime):
            if not is_non_graph_export_name(name) and not is_non_graph_export_name(
                    os.path.basename(md_path)):
                return 'trivial'
        if is_data_asset_export_name(name) or is_data_asset_export_name(
                os.path.basename(md_path)):
            loaded = _load_any_asset(package) or _load_any_asset(
                '{}.{}'.format(package, name))
            if loaded is None:
                failed.append(package or name)
                return
            result = export_data_asset(
                loaded,
                out_dir=root,
                notify=False,
                copy_clipboard=False,
                package=package,
            )
            if result is None:
                return 'skip'
            if result.get('trivial'):
                return 'trivial'
            if result.get('skipped'):
                return 'uptodate'
            wrote.append(result.get('md'))
            return 'ok'
        blueprint = _load_blueprint(package) or _load_blueprint('{}.{}'.format(package, name))
        if blueprint is None:
            failed.append(package or name)
            return
        result = export_all(
            blueprint,
            out_dir=root,
            notify=False,
            copy_clipboard=False,
            write_probe=False,
        )
        if result is None:
            return 'skip'
        if result.get('trivial'):
            return 'trivial'
        if result.get('skipped'):
            return 'uptodate'
        wrote.append(result.get('md'))
        return 'ok'

    def _loop(task):
        skip_count = 0
        uptodate_count = 0
        trivial_count = 0
        for asset in assets:
            if task is not None:
                try:
                    if task.should_cancel():
                        return skip_count, uptodate_count, trivial_count, True
                    label = str(getattr(asset, 'asset_name', '') or '')
                    task.enter_progress_frame(1, label)
                except Exception:
                    pass
            try:
                status = _one(asset)
            except Exception as error:
                failed.append('{} ({})'.format(_asset_package_name(asset), error))
                continue
            if status == 'skip':
                skip_count += 1
            elif status == 'uptodate':
                uptodate_count += 1
            elif status == 'trivial':
                trivial_count += 1
        return skip_count, uptodate_count, trivial_count, False

    task_cls = getattr(unreal, 'ScopedSlowTask', None)
    progress_title = 'Export {} — Blueprint graphs'.format(project_name)
    if task_cls is not None and total:
        try:
            with task_cls(total, progress_title) as task:
                try:
                    task.make_dialog(True)
                except Exception:
                    pass
                skipped, skipped_uptodate, skipped_trivial, cancelled = _loop(task)
        except Exception as error:
            _log_error('Export {}: progress dialog failed ({}); running without it'.format(
                project_name, error))
            skipped, skipped_uptodate, skipped_trivial, cancelled = _loop(None)
    else:
        skipped, skipped_uptodate, skipped_trivial, cancelled = _loop(None)

    message = 'Export {}: wrote {} of {} Blueprints\n{}'.format(
        project_name, len(wrote), total, root)
    if skipped_uptodate:
        message += '\nUp to date (skipped): {}'.format(skipped_uptodate)
    if skipped_trivial:
        message += '\nRoot-only (skipped): {}'.format(skipped_trivial)
    if skipped:
        message += '\nSkipped (no graphs): {}'.format(skipped)
    if failed:
        message += '\nFailed: {}'.format(len(failed))
    if cancelled:
        message += '\nCancelled.'
    _log(message)
    for item in failed[:20]:
        _log_error('Export {} failed: {}'.format(project_name, item))
    if notify:
        _notify_folder(root, message)
    return {
        'root': root,
        'wrote': wrote,
        'skipped': skipped,
        'skipped_uptodate': skipped_uptodate,
        'skipped_trivial': skipped_trivial,
        'failed': failed,
        'cancelled': cancelled,
        'total': total,
    }


def run_editor_selftest(notify=False):
    """Spike the 5.8 graph APIs and export EventGraph + one other graph if present.

    Call from the editor Python console after `register_menu()`. Writes a JSON report
    next to the export files.
    """
    if unreal is None:
        raise RuntimeError('run_editor_selftest requires the Unreal editor')
    report = {'spike': spike_api(), 'exports': [], 'errors': []}
    blueprint = None
    graph_names = []
    preferred = ()
    for path in preferred:
        blueprint = _load_blueprint(path)
        if blueprint is not None:
            graph_names = _list_graph_names(blueprint)
            if graph_names:
                break
    if blueprint is None or not graph_names:
        try:
            registry = unreal.AssetRegistryHelpers.get_asset_registry()
            assets = registry.get_assets_by_class(
                unreal.TopLevelAssetPath('/Script/Engine', 'Blueprint'), True)
        except Exception as error:
            report['errors'].append(str(error))
            assets = []
        for asset in list(assets or [])[:120]:
            try:
                object_path = str(asset.get_full_name().split(' ')[-1])
                candidate = _load_blueprint(object_path)
            except Exception:
                candidate = None
            if candidate is None:
                continue
            names = _list_graph_names(candidate)
            if 'EventGraph' in names and len(names) >= 2:
                blueprint = candidate
                graph_names = names
                break
            if blueprint is None and names:
                blueprint = candidate
                graph_names = names
    report['blueprint'] = blueprint.get_path_name() if blueprint else None
    report['graphs'] = graph_names
    if blueprint is None:
        report['errors'].append('no Blueprint found to export')
        out_dir = default_output_dir()
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        report_path = os.path.join(out_dir, 'selftest.json')
        with open(report_path, 'w', encoding='utf-8') as handle:
            json.dump(report, handle, indent=2)
        report['report_path'] = report_path
        return report
    targets = []
    if 'EventGraph' in graph_names:
        targets.append('EventGraph')
    for name in graph_names:
        if name not in targets:
            targets.append(name)
        if len(targets) >= 2:
            break
    for name in targets:
        try:
            result = export_graph(blueprint, name, notify=notify)
            if result:
                report['exports'].append({
                    'graph': name,
                    'md': result['md'],
                    'nodes': len(result['dump'].get('nodes') or []),
                    'edges': len(result['dump'].get('edges') or []),
                    'comments': len(result['dump'].get('comments') or []),
                })
        except Exception as error:
            report['errors'].append('{}: {}'.format(name, error))
    out_dir = default_output_dir()
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    report_path = os.path.join(out_dir, 'selftest.json')
    with open(report_path, 'w', encoding='utf-8') as handle:
        json.dump(report, handle, indent=2)
    report['report_path'] = report_path
    _log('Export Graph selftest: {}'.format(report_path))
    return report


def export_graph(blueprint, graph_name, out_dir=None, notify=True):
    """Export one graph of an already-loaded Blueprint."""
    if unreal is None:
        raise RuntimeError('export_graph requires the Unreal editor')
    graph = _find_graph(blueprint, graph_name)
    if graph is None:
        _log_error('Export Graph: graph {} not found on {}'.format(graph_name, blueprint.get_name()))
        return None
    if is_anim_pose_graph_name(graph_name):
        _log('Export Graph: skip pose graph {} / {}'.format(
            blueprint.get_name(), graph_name))
        return None
    dump = collect_graph_dump(blueprint, graph)
    if dump_is_trivial(dump):
        _log('Export Graph: skip root-only {} / {}'.format(
            blueprint.get_name(), graph_name))
        if notify:
            _notify_folder(
                None,
                'Skipped root-only graph {} / {}'.format(
                    blueprint.get_name(), graph_name))
        return None
    target_dir = out_dir or default_output_dir()
    md_path = write_export_files(dump, target_dir)
    probe_path = _write_probe_if_empty(dump, target_dir)
    mermaid = dump_to_mermaid(dump)
    _copy_to_clipboard(mermaid)
    _log('Export Graph: module={} wrote {} ({} nodes, {} edges)'.format(
        __file__, md_path, len(dump.get('nodes') or []), len(dump.get('edges') or [])))
    if probe_path:
        _log_error('Export Graph: 0 nodes; wrote probe {}'.format(probe_path))
    if notify:
        _notify(md_path, mermaid, probe_path=probe_path)
    return {'md': md_path, 'dump': dump, 'probe': probe_path}


def _generated_class(blueprint):
    lib = getattr(unreal, 'BlueprintEditorLibrary', None)
    if lib is not None and hasattr(lib, 'generated_class'):
        try:
            generated = lib.generated_class(blueprint)
            if generated is not None:
                return generated
        except Exception:
            pass
    getter = getattr(blueprint, 'generated_class', None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            return None
    return None


def _blueprint_parent_class(blueprint):
    lib = getattr(unreal, 'BlueprintEditorLibrary', None)
    if lib is not None and hasattr(lib, 'get_blueprint_parent_class'):
        try:
            parent = lib.get_blueprint_parent_class(blueprint)
            if parent is not None:
                return parent
        except Exception:
            pass
    return _try_uproperty(blueprint, ['parent_class', 'ParentClass'])


def _cdo_of_class(cls):
    if cls is None or unreal is None:
        return None
    try:
        return unreal.get_default_object(cls)
    except Exception:
        return None


def _read_named_property(obj, names):
    if obj is None:
        return None
    for name in names:
        try:
            return obj.get_editor_property(name)
        except Exception:
            pass
        try:
            if hasattr(obj, name):
                return getattr(obj, name)
        except Exception:
            pass
    return None


def _collect_interface_names(blueprint):
    raw = _try_uproperty(blueprint, ['implemented_interfaces', 'ImplementedInterfaces'])
    names = []
    for item in _as_list(raw):
        iface = _read_named_property(item, ['interface', 'Interface'])
        text = format_export_value(iface)
        if text:
            names.append(text)
    return names


def collect_class_settings(blueprint):
    """Class Settings panel fields that are set / non-default."""
    rows = []
    parent = _blueprint_parent_class(blueprint)
    parent_text = format_export_value(parent)
    if parent_text:
        rows.append({'name': 'Parent Class', 'value': parent_text})
    pairs = (
        ('Display Name', ['blueprint_display_name', 'BlueprintDisplayName']),
        ('Description', ['blueprint_description', 'BlueprintDescription']),
        ('Category', ['blueprint_category', 'BlueprintCategory']),
        ('Namespace', ['blueprint_namespace', 'BlueprintNamespace']),
        ('Hide Categories', ['hide_categories', 'HideCategories']),
        ('Imported Namespaces', ['imported_namespaces', 'ImportedNamespaces']),
    )
    for label, names in pairs:
        text = format_export_value(_read_named_property(blueprint, names))
        if text:
            rows.append({'name': label, 'value': text})
    interfaces = _collect_interface_names(blueprint)
    if interfaces:
        rows.append({'name': 'Interfaces', 'value': ', '.join(interfaces)})
    flags = (
        ('Generate Abstract Class', ['generate_abstract_class', 'b_generate_abstract_class', 'bGenerateAbstractClass']),
        ('Generate Const Class', ['generate_const_class', 'b_generate_const_class', 'bGenerateConstClass']),
        ('Deprecated', ['deprecate', 'b_deprecate', 'bDeprecate']),
        ('Run Construction Script On Drag', [
            'run_construction_script_on_drag', 'b_run_construction_script_on_drag',
            'bRunConstructionScriptOnDrag',
        ]),
    )
    for label, names in flags:
        value = _read_named_property(blueprint, names)
        if value is True:
            rows.append({'name': label, 'value': 'true'})
    compile_mode = format_export_value(_read_named_property(
        blueprint, ['compile_mode', 'CompileMode']))
    if compile_mode and compile_mode not in ('Default', 'EBlueprintCompileMode.DEFAULT', '0'):
        rows.append({'name': 'Compile Mode', 'value': compile_mode})
    return rows


def member_variable_basename(name):
    """Strip inherited-class prefix from ListMemberVariableNames entries."""
    text = str(name or '').strip()
    if not text:
        return ''
    if ':' in text:
        text = text.rsplit(':', 1)[-1]
    if '/' in text:
        text = text.rsplit('.', 1)[-1]
    return text


def _to_snake_property_name(name):
    text = member_variable_basename(name)
    if not text:
        return ''
    if text[:1] == 'b' and len(text) > 1 and text[1:2].isupper():
        text = text[1:]
    stepped = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', text)
    stepped = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', stepped)
    return stepped.lower()


def property_name_candidates(name):
    """C++ / Python / bool-prefix spellings for get_editor_property."""
    base = member_variable_basename(name)
    if not base:
        return []
    out = []
    seen = set()

    def add(item):
        if item and item not in seen:
            seen.add(item)
            out.append(item)

    add(base)
    snake = _to_snake_property_name(base)
    add(snake)
    if base[:1] == 'b' and len(base) > 1 and base[1:2].isupper():
        rest = base[1:]
        add(rest)
        add(_to_snake_property_name(rest))
        add('b_' + _to_snake_property_name(rest))
    elif snake and not snake.startswith('b_'):
        add('b_' + snake)
        pascal = ''.join(part[:1].upper() + part[1:] for part in snake.split('_') if part)
        add('b' + pascal)
    return out


def display_member_variable_name(name):
    """Details-panel style label: bEnableTick → Enable Tick."""
    base = member_variable_basename(name)
    if not base:
        return ''
    if base[:1] == 'b' and len(base) > 1 and base[1:2].isupper():
        base = base[1:]
    spaced = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', base)
    spaced = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', spaced)
    return spaced.replace('_', ' ')


def _should_skip_cdo_name(name):
    base = member_variable_basename(name)
    if not base or base.startswith('_'):
        return True
    snake = _to_snake_property_name(base)
    if snake in CDO_SKIP_NAMES or base.lower() in CDO_SKIP_NAMES:
        return True
    if snake.startswith('on_') or base.lower().startswith('on_'):
        return True
    return False


def keep_class_default_member(name):
    """Drop Engine/UMG inherited members before probing get_editor_property."""
    text = str(name or '')
    if not text or _should_skip_cdo_name(text):
        return False
    lower = text.replace('\\', '/').lower()
    for marker in _SKIP_INHERITED_MEMBER_MARKERS:
        if marker in lower:
            return False
    return True


def read_cdo_property(obj, name):
    """Read a CDO/UPROPERTY. Returns (found, value); found is True for False/0."""
    if obj is None:
        return False, None
    getter = getattr(obj, 'get_editor_property', None)
    if not callable(getter):
        return False, None
    base = member_variable_basename(name)
    candidates = [base]
    snake = _to_snake_property_name(base)
    if snake and snake not in candidates:
        candidates.append(snake)
    if base[:1] == 'b' and len(base) > 1 and base[1:2].isupper():
        rest_snake = _to_snake_property_name(base[1:])
        if rest_snake and rest_snake not in candidates:
            candidates.append(rest_snake)
    for candidate in candidates:
        try:
            return True, getter(candidate)
        except Exception:
            continue
    return False, None


def _new_variable_meta(blueprint):
    """VarName → {friendly, default} from UBlueprint.NewVariables (DefaultValue often empty)."""
    meta = {}
    raw = _try_uproperty(blueprint, ['new_variables', 'NewVariables'])
    for var in _as_list(raw):
        name = member_variable_basename(
            _read_named_property(var, ['var_name', 'VarName']))
        if not name:
            continue
        friendly = format_export_value(_read_named_property(
            var, ['friendly_name', 'FriendlyName']))
        default = _read_named_property(var, ['default_value', 'DefaultValue'])
        meta[name] = {'friendly': friendly, 'default': default}
    return meta


def _list_member_variable_names(blueprint, include_inherited=True):
    """BlueprintEditorLibrary.ListMemberVariableNames (return-value or out-array)."""
    global _LIST_MEMBERS_USES_FLAG, _LIST_MEMBERS_OUT_ARRAY
    if unreal is None or blueprint is None:
        return []
    lib = getattr(unreal, 'BlueprintEditorLibrary', None)
    method = None
    if lib is not None:
        method = getattr(lib, 'list_member_variable_names', None)
    call = method
    if call is None:
        method = getattr(blueprint, 'list_member_variable_names', None)
        if method is None:
            return []

        def _bound(*args):
            return method(*args[1:] if args and args[0] is blueprint else args)

        call = _bound

    def _normalize(result):
        return [str(item) for item in _as_list(result) if str(item)]

    if not _LIST_MEMBERS_OUT_ARRAY:
        try:
            if _LIST_MEMBERS_USES_FLAG:
                return _normalize(call(blueprint, include_inherited))
            return _normalize(call(blueprint))
        except TypeError:
            if _LIST_MEMBERS_USES_FLAG:
                _LIST_MEMBERS_USES_FLAG = False
                try:
                    return _normalize(call(blueprint))
                except TypeError:
                    _LIST_MEMBERS_OUT_ARRAY = True
            else:
                _LIST_MEMBERS_OUT_ARRAY = True
        except Exception:
            return []

    buckets = []
    if hasattr(unreal, 'Array'):
        for elem in (str, getattr(unreal, 'Name', None)):
            if elem is None:
                continue
            try:
                buckets.append(unreal.Array(elem))
            except Exception:
                pass
    buckets.append([])
    for bucket in buckets:
        try:
            call(blueprint, bucket, include_inherited)
            items = _normalize(bucket)
            if items:
                return items
        except TypeError:
            try:
                call(blueprint, bucket)
                return _normalize(bucket)
            except Exception:
                continue
        except Exception:
            continue
    return []


def build_class_default_rows(names, cdo, variable_meta=None):
    """Class Defaults rows from member names + CDO values (False/0 included)."""
    meta = variable_meta or {}
    rows = []
    seen = set()
    for name in names:
        if len(rows) >= CLASS_DEFAULTS_MAX_ROWS:
            break
        base = member_variable_basename(name)
        if not base or _should_skip_cdo_name(base):
            continue
        key = base.lower()
        if key in seen:
            continue
        found, current = read_cdo_property(cdo, base)
        if not found:
            stored = meta.get(base) or meta.get(name)
            if stored is not None and stored.get('default') is not None:
                current = stored.get('default')
                found = str(current) != ''
        if not found:
            continue
        current_text = format_export_value(current)
        if current is None and not current_text:
            continue
        label = ''
        stored = meta.get(base) or meta.get(name)
        if stored:
            label = stored.get('friendly') or ''
        if not label:
            label = display_member_variable_name(base)
        seen.add(key)
        rows.append({'name': label, 'value': current_text})
    return rows[:CLASS_DEFAULTS_MAX_ROWS]


def collect_class_defaults(blueprint):
    """Class Defaults: BP members + non-Engine inherited CDO values."""
    meta = _new_variable_meta(blueprint)
    listed = _list_member_variable_names(blueprint, include_inherited=True)
    extras = list(meta.keys())
    generated = _generated_class(blueprint)
    cdo = _cdo_of_class(generated)
    names = []
    seen = set()
    for name in extras + listed:
        if not keep_class_default_member(name):
            continue
        base = member_variable_basename(name)
        key = base.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        names.append(name)
    stem = ''
    getter = getattr(blueprint, 'get_name', None)
    if callable(getter):
        try:
            stem = getter()
        except Exception:
            stem = ''
    if is_gas_export_name(stem):
        for extra in GAS_CDO_EXTRA_BASES:
            if not keep_class_default_member(extra):
                continue
            base = member_variable_basename(extra)
            key = base.lower()
            if not key or key in seen:
                continue
            seen.add(key)
            names.append(extra)
    return build_class_default_rows(names, cdo, meta)


def _looks_like_actor_component(value):
    if value is None or isinstance(value, (str, bytes, bytearray, bool, int, float)):
        return False
    if unreal is not None:
        class_cls = getattr(unreal, 'Class', None)
        if class_cls is not None:
            try:
                if isinstance(value, class_cls):
                    return False
            except Exception:
                pass
        cls = getattr(unreal, 'ActorComponent', None)
        if cls is not None:
            try:
                if isinstance(value, cls):
                    return True
            except Exception:
                pass
    if not callable(getattr(value, 'get_editor_property', None)):
        return False
    return 'Component' in _gas_type_name(value)


def _component_instance_name(comp):
    getter = getattr(comp, 'get_name', None)
    if callable(getter):
        try:
            text = str(getter() or '')
            if text:
                return export_asset_stem(text) or text
        except Exception:
            pass
    return _gas_type_name(comp)


def should_skip_component_dump(comp):
    """True for Engine Scene/Movement/Timeline — dir() on those AV'd UE 5.8."""
    if comp is None:
        return True
    if unreal is not None:
        for type_name in COMPONENT_DUMP_SKIP_UNREAL_TYPES:
            cls = getattr(unreal, type_name, None)
            if cls is None:
                continue
            try:
                if isinstance(comp, cls):
                    return True
            except Exception:
                pass
    path = ''
    get_class = getattr(comp, 'get_class', None)
    if callable(get_class):
        try:
            cls = get_class()
            get_path = getattr(cls, 'get_path_name', None)
            if callable(get_path):
                path = str(get_path() or '')
        except Exception:
            path = ''
    lower_path = path.replace('\\', '/').lower()
    if '/script/engine.' in lower_path or '/script/engine/' in lower_path:
        return True
    class_name = _data_asset_class_name(comp) or _gas_type_name(comp) or ''
    instance = _component_instance_name(comp) or ''
    haystack = '{} {}'.format(class_name, instance)
    for marker in COMPONENT_DUMP_SKIP_CLASS_MARKERS:
        if marker in haystack:
            return True
    return False


def _iter_blueprint_component_objects(blueprint):
    found = []
    seen = set()

    def add(comp):
        if comp is None:
            return
        if should_skip_component_dump(comp):
            return
        if not _looks_like_actor_component(comp):
            return
        try:
            name = _component_instance_name(comp)
        except Exception:
            name = ''
        if name.startswith(('SKEL_', 'REINST_', 'TRASH_', 'DEAD_')):
            return
        try:
            key = comp.get_path_name()
        except Exception:
            key = id(comp)
        if key in seen:
            return
        seen.add(key)
        found.append(comp)

    generated = _generated_class(blueprint)
    cdo = _cdo_of_class(generated)
    if cdo is not None:
        for name in _list_member_variable_names(blueprint, include_inherited=True):
            found_prop, value = read_cdo_property(cdo, name)
            if found_prop:
                add(value)
    # No construction-script node walk: that AV'd UE 5.8 on LevelButton
    # after Class Defaults already read the same CDO members successfully.
    _log('Export All: components-cdo-done n={}'.format(len(found)))
    return found


def collect_component_entries(components, property_names=None):
    """Markdown-ready component dicts: name, class, props (engine noise skipped)."""
    entries = []
    for comp in components or []:
        if comp is None or should_skip_component_dump(comp):
            continue
        _log('Export All: dump-component {}'.format(_component_instance_name(comp)))
        props = collect_data_asset_json(
            comp, names=property_names, skip_engine_component=True)
        if not props:
            continue
        entries.append({
            'name': _component_instance_name(comp),
            'class': _data_asset_class_name(comp),
            'props': props,
        })
    entries.sort(key=lambda item: str(item.get('name') or '').lower())
    return entries[:64]


def collect_blueprint_components(blueprint):
    """CDO UPROPERTY actor components only. SCS walks AV'd UE 5.8 on LevelButton."""
    try:
        return collect_component_entries(_iter_blueprint_component_objects(blueprint))
    except Exception as error:
        label = '?'
        getter = getattr(blueprint, 'get_name', None)
        if callable(getter):
            try:
                label = getter() or label
            except Exception:
                pass
        _log_error('Export All: components failed on {}: {}'.format(label, error))
        return []


def _data_asset_class_name(obj):
    get_class = getattr(obj, 'get_class', None)
    if callable(get_class):
        try:
            cls = get_class()
            name_fn = getattr(cls, 'get_name', None)
            if callable(name_fn):
                text = pretty_export_text(name_fn())
                if text:
                    return text
        except Exception:
            pass
    text = pretty_export_text(_gas_type_name(obj))
    return text or ''


def export_data_asset(obj, out_dir=None, notify=True, copy_clipboard=None,
                      package=None):
    """Dump a `DA_*` DataAsset (or its Blueprint CDO) to `{Asset}.md` JSON."""
    if unreal is None:
        raise RuntimeError('export_data_asset requires the Unreal editor')
    if obj is None:
        return None
    if copy_clipboard is None:
        copy_clipboard = notify
    source = obj
    stem = ''
    getter = getattr(obj, 'get_name', None)
    if callable(getter):
        try:
            stem = export_asset_stem(getter())
        except Exception:
            stem = ''
    bp = _as_blueprint(obj)
    if bp is not None:
        stem = export_asset_stem(bp.get_name()) or stem
        generated = _generated_class(bp)
        cdo = _cdo_of_class(generated)
        if cdo is not None:
            source = cdo
        if not package:
            package = _package_path_from_blueprint(bp)
    if not package:
        package = _package_path_from_blueprint(obj)
    root = out_dir or default_output_dir()
    if package:
        md_path = export_md_path(package, root)
    else:
        md_path = os.path.join(root, export_markdown_filename(stem or 'DA'))
    source_mtime = package_source_mtime(package)
    if not should_refresh_export(md_path, source_mtime):
        strip_legacy_yaml_header(md_path, source_mtime)
        if existing_da_export_is_keepable(md_path):
            body = ''
            if copy_clipboard and os.path.isfile(md_path):
                try:
                    with open(md_path, encoding='utf-8') as handle:
                        body = handle.read()
                    _copy_to_clipboard(body)
                except Exception:
                    body = ''
            _log('Export DA: skip (mtime match) {}'.format(md_path))
            if notify:
                _notify(md_path, body or '(unchanged)')
            clear_trivial_skip(md_path)
            return {'md': md_path, 'data': {}, 'skipped': True}
        if existing_export_is_trivial(md_path):
            deleted = record_trivial_export(md_path, source_mtime, root)
            return {
                'md': md_path,
                'data': {},
                'skipped': True,
                'trivial': True,
                'deleted': deleted,
            }
    data = collect_data_asset_json(source)
    class_name = _data_asset_class_name(source)
    if class_name:
        payload = {'_Class': class_name}
        payload.update(data)
        data = payload
    target_dir = os.path.dirname(md_path) or root
    if not data:
        deleted = record_trivial_export(md_path, source_mtime, root)
        _log('Export DA: no properties on {}'.format(stem or package))
        if notify:
            _notify_folder(None, 'No properties on {}.'.format(stem or package))
        return {
            'md': md_path,
            'data': {},
            'skipped': True,
            'trivial': True,
            'deleted': deleted,
        }
    clear_trivial_skip(md_path)
    md_path = write_data_asset_export_file(
        stem, data, target_dir, filename=os.path.basename(md_path))
    stamp_export_mtime(md_path, source_mtime)
    body = assemble_data_asset_markdown(stem, data)
    if copy_clipboard:
        _copy_to_clipboard(body)
    _log('Export DA: module={} wrote {}'.format(__file__, md_path))
    if notify:
        _notify(md_path, body)
    return {'md': md_path, 'data': data, 'skipped': False}


def export_all(blueprint, out_dir=None, notify=True, copy_clipboard=None,
               filename=None, write_probe=False):
    """Export every graph of a Blueprint into the Content-mirrored `{Asset}.md`."""
    if unreal is None:
        raise RuntimeError('export_all requires the Unreal editor')
    if copy_clipboard is None:
        copy_clipboard = notify
    names = ordered_graph_names(_list_graph_names(blueprint))
    package = _package_path_from_blueprint(blueprint)
    root = out_dir or default_output_dir()
    asset_name = blueprint.get_name()
    if filename:
        md_path = os.path.join(root, filename)
    elif package:
        md_path = export_md_path(package, root)
    else:
        md_path = os.path.join(root, export_markdown_filename(asset_name))
    source_mtime = package_source_mtime(package)
    gas_named = is_gas_export_name(asset_name) or is_gas_export_name(
        os.path.basename(md_path))
    if trivial_skip_is_current(md_path, source_mtime) and not gas_named:
        _log('Export All: skip (root-only) {}'.format(md_path))
        if notify:
            _notify_folder(
                None,
                'No meaningful graphs on {} (root-only events/entries).'.format(
                    asset_name))
        return {
            'md': md_path,
            'dumps': [],
            'probes': [],
            'skipped': True,
            'trivial': True,
        }
    if not should_refresh_export(md_path, source_mtime):
        strip_legacy_yaml_header(md_path, source_mtime)
        if existing_export_is_trivial(md_path):
            if existing_non_graph_export_is_keepable(md_path):
                body = ''
                if copy_clipboard and os.path.isfile(md_path):
                    try:
                        with open(md_path, encoding='utf-8') as handle:
                            body = handle.read()
                        _copy_to_clipboard(body)
                    except Exception:
                        body = ''
                _log('Export All: skip (mtime match) {}'.format(md_path))
                if notify:
                    _notify(md_path, body or '(unchanged)')
                clear_trivial_skip(md_path)
                return {'md': md_path, 'dumps': [], 'probes': [], 'skipped': True}
            deleted = record_trivial_export(md_path, source_mtime, root)
            _log('Export All: removed root-only {}'.format(md_path))
            if notify:
                _notify_folder(
                    None,
                    'Removed root-only export:\n{}'.format(md_path))
            return {
                'md': md_path,
                'dumps': [],
                'probes': [],
                'skipped': True,
                'trivial': True,
                'deleted': deleted,
            }
        if not existing_export_has_trivial_block(md_path):
            body = ''
            if copy_clipboard and os.path.isfile(md_path):
                try:
                    with open(md_path, encoding='utf-8') as handle:
                        body = handle.read()
                    _copy_to_clipboard(body)
                except Exception:
                    body = ''
            _log('Export All: skip (mtime match) {}'.format(md_path))
            if notify:
                _notify(md_path, body or '(unchanged)')
            clear_trivial_skip(md_path)
            return {'md': md_path, 'dumps': [], 'probes': [], 'skipped': True}
        _log('Export All: rewrite to drop stub/pose graphs {}'.format(md_path))
    _log('Export All: collecting {} ({} graphs)'.format(asset_name, len(names)))
    dumps = []
    probes = []
    target_dir = os.path.dirname(md_path) or root
    for name in names:
        if is_anim_pose_graph_name(name):
            continue
        _log('Export All: graph {} / {}'.format(asset_name, name))
        graph = _find_graph(blueprint, name)
        if graph is None:
            _log_error('Export All: graph {} not found on {}'.format(name, asset_name))
            continue
        dump = collect_graph_dump(blueprint, graph)
        dumps.append(dump)
        if write_probe:
            probe = _write_probe_if_empty(dump, target_dir)
            if probe:
                probes.append(probe)
    _log('Export All: class {}'.format(asset_name))
    kept = meaningful_dumps(dumps)
    class_settings = collect_class_settings(blueprint)
    class_defaults = collect_class_defaults(blueprint)
    _log('Export All: components {}'.format(asset_name))
    components = collect_blueprint_components(blueprint)
    if not kept:
        if keep_class_only_export(
                asset_name, class_settings, class_defaults, components):
            kept = []
        else:
            if not names:
                _log_error('Export All: no graphs on {}'.format(asset_name))
            deleted = record_trivial_export(md_path, source_mtime, root)
            _log('Export All: no meaningful graphs on {} ({} dumpable)'.format(
                asset_name, len(dumps)))
            if notify:
                _notify_folder(
                    None,
                    'No meaningful graphs on {}.'.format(asset_name))
            return {
                'md': md_path,
                'dumps': [],
                'probes': probes,
                'skipped': True,
                'trivial': True,
                'deleted': deleted,
            }
    dumps = kept
    clear_trivial_skip(md_path)
    md_path = write_combined_export_file(
        dumps, target_dir, filename=os.path.basename(md_path),
        class_settings=class_settings, class_defaults=class_defaults,
        components=components)
    stamp_export_mtime(md_path, source_mtime)
    body = assemble_blueprint_markdown(
        dumps, class_settings=class_settings, class_defaults=class_defaults,
        components=components)
    if copy_clipboard:
        _copy_to_clipboard(body)
    _log('Export All: module={} wrote {} ({} graphs)'.format(__file__, md_path, len(dumps)))
    if probes:
        _log_error('Export All: {} empty graph(s); probes: {}'.format(
            len(probes), '; '.join(probes)))
    if notify:
        _notify(md_path, body, probe_path=probes[0] if probes else None)
    return {'md': md_path, 'dumps': dumps, 'probes': probes, 'skipped': False}


def auto_export_blueprint(blueprint):
    """Silent Export All used by the save hook. No dialog, no clipboard."""
    if unreal is None or blueprint is None:
        return None
    hooks = _hooks()
    if hooks.get('exporting'):
        return None
    try:
        path = blueprint.get_path_name()
    except Exception:
        path = str(blueprint)
    now = time.time()
    last = hooks.get('last_export_at', {}).get(path)
    if last is not None and (now - last) < AUTO_EXPORT_DEBOUNCE_SEC:
        return None
    hooks.setdefault('last_export_at', {})[path] = now
    hooks['exporting'] = True
    try:
        _log('Export All (auto): {}'.format(blueprint.get_name()))
        return export_all(blueprint, notify=False, copy_clipboard=False)
    except Exception as error:
        _log_error('Export All (auto) failed: {}'.format(error))
        return None
    finally:
        hooks['exporting'] = False


def auto_export_data_asset(obj):
    """Silent DA_ JSON export used by the save hook."""
    if unreal is None or obj is None:
        return None
    hooks = _hooks()
    if hooks.get('exporting'):
        return None
    try:
        path = obj.get_path_name()
    except Exception:
        path = str(obj)
    now = time.time()
    last = hooks.get('last_export_at', {}).get(path)
    if last is not None and (now - last) < AUTO_EXPORT_DEBOUNCE_SEC:
        return None
    hooks.setdefault('last_export_at', {})[path] = now
    hooks['exporting'] = True
    try:
        _log('Export DA (auto): {}'.format(getattr(obj, 'get_name', lambda: obj)()))
        return export_data_asset(obj, notify=False, copy_clipboard=False)
    except Exception as error:
        _log_error('Export DA (auto) failed: {}'.format(error))
        return None
    finally:
        hooks['exporting'] = False


def collect_graph_dump(blueprint, graph):
    """Walk an EdGraph into the dump dict consumed by the writers."""
    lib = unreal.BlueprintEditorLibrary
    nodes_out = []
    comments_out = []
    edges_out = []
    node_ids = {}
    knots = set()
    probe = {}

    populated = _resolve_graph_with_nodes(blueprint, graph)
    if populated is not None:
        graph = populated
    raw_nodes = _collect_graph_nodes(blueprint, graph, probe)
    for node in raw_nodes:
        class_name = _class_name(node)
        if class_name in COMMENT_CLASSES or isinstance(node, getattr(unreal, 'EdGraphNode_Comment', type(None))):
            comments_out.append(_dump_comment(node, lib))
            continue
        if class_name in KNOT_CLASSES:
            knots.add(_node_key(node))
            continue
        record = _dump_k2_node(node, lib)
        node_ids[_node_key(node)] = record['id']
        nodes_out.append(record)

    for node in raw_nodes:
        if _node_key(node) in knots:
            continue
        if _class_name(node) in COMMENT_CLASSES:
            continue
        src_id = node_ids.get(_node_key(node))
        if not src_id:
            continue
        for pin in _list_output_pins(node, lib):
            if not _pin_valid(pin):
                continue
            for dest_node, dest_pin in _follow_pin(pin, knots):
                dst_id = node_ids.get(_node_key(dest_node))
                if not dst_id:
                    continue
                edges_out.append({
                    'from': src_id,
                    'to': dst_id,
                    'from_pin': _pin_name(pin),
                    'to_pin': _pin_name(dest_pin),
                    'kind': 'exec' if _pin_is_exec(pin) else 'data',
                })

    for comment in comments_out:
        comment['member_ids'] = _comment_member_ids(comment, lib, node_ids)

    warnings = []
    if not raw_nodes:
        warnings.append(
            'graph {} has 0 nodes via BlueprintGraphEditor.list_all_nodes + Nodes UPROPERTY. probe={}'.format(
                graph.get_name(), json.dumps(probe, default=str)[:800]))
    elif not nodes_out and not comments_out:
        warnings.append('graph {} had {} objects but none dumped as K2/comment nodes'.format(
            graph.get_name(), len(raw_nodes)))
    return {
        'blueprint': blueprint.get_name(),
        'blueprint_path': blueprint.get_path_name(),
        'graph': str(graph.get_name()),
        'nodes': nodes_out,
        'edges': edges_out,
        'comments': comments_out,
        'raw_node_count': len(raw_nodes),
        'warnings': warnings,
        'probe': probe,
    }


def _dump_k2_node(node, lib):
    title = _node_title(node, lib)
    pos = _node_pos(node, lib)
    size = _node_size(node, lib)
    literals = {}
    for pin in _list_input_pins(node, lib):
        if not _pin_valid(pin):
            continue
        if _pin_connected(pin):
            continue
        value = _pin_value(pin)
        if value:
            literals[_pin_name(pin)] = value
    return {
        'id': node.get_name(),
        'title': title,
        'class': _class_name(node),
        'x': pos[0],
        'y': pos[1],
        'w': size[0],
        'h': size[1],
        'kind': 'node',
        'literals': literals,
    }


def _dump_comment(node, lib):
    title = ''
    if hasattr(lib, 'get_comment_text'):
        try:
            title = lib.get_comment_text(node) or ''
        except Exception:
            title = ''
    if not title:
        title = str(getattr(node, 'node_comment', '') or node.get_name())
    pos = _node_pos(node, lib)
    width = float(getattr(node, 'node_width', 0) or 0)
    height = float(getattr(node, 'node_height', 0) or 0)
    if width <= 0 or height <= 0:
        size = _node_size(node, lib)
        width = width or size[0] or 320
        height = height or size[1] or 160
    return {
        'id': node.get_name(),
        'title': title,
        'x': pos[0],
        'y': pos[1],
        'w': width,
        'h': height,
        'ue_node': node,
        'member_ids': [],
    }


def _comment_member_ids(comment, lib, node_ids):
    node = comment.pop('ue_node', None)
    if node is None or not hasattr(lib, 'get_nodes_in_comment'):
        return []
    try:
        members = lib.get_nodes_in_comment(node) or []
    except Exception:
        return []
    ids = []
    for member in members:
        mapped = node_ids.get(_node_key(member))
        if mapped:
            ids.append(mapped)
    return ids


def _follow_pin(pin, knots, _seen=None):
    """Yield (dest_node, dest_pin) skipping knot nodes."""
    seen = _seen if _seen is not None else set()
    pin_key = _pin_key(pin)
    if pin_key in seen:
        return
    seen.add(pin_key)
    for other in _connected_pins(pin):
        if not _pin_valid(other):
            continue
        owner = _pin_owner(other)
        if owner is None:
            continue
        if _node_key(owner) in knots or _class_name(owner) in KNOT_CLASSES:
            for knot_out in _list_output_pins(owner, unreal.BlueprintEditorLibrary):
                if _pin_key(knot_out) == _pin_key(other):
                    continue
                for item in _follow_pin(knot_out, knots, seen):
                    yield item
            continue
        yield owner, other


def _try_uproperty(obj, names):
    """Read a UPROPERTY by C++ or Python name. Editor-only arrays often lack a Python `.nodes` attr."""
    if obj is None:
        return None
    seen = []
    for name in names:
        if name in seen:
            continue
        seen.append(name)
        try:
            value = obj.get_editor_property(name)
            if value is not None:
                return value
        except Exception:
            pass
        try:
            value = getattr(obj, name, None)
            if value is not None:
                return value
        except Exception:
            pass
    return None


def _invoke_out_array(obj, method_name, element_cls=None):
    """Call a UE function that returns TArray or fills an out TArray."""
    method = getattr(obj, method_name, None)
    if method is None:
        return []
    try:
        result = method()
        if result is not None:
            return _as_list(result)
    except TypeError:
        pass
    except Exception:
        pass
    bucket = []
    if unreal is not None and element_cls is not None:
        try:
            bucket = unreal.Array(element_cls)
        except Exception:
            bucket = []
    try:
        result = method(bucket)
        items = _as_list(result)
        if items:
            return items
        return _as_list(bucket)
    except Exception:
        return []


def _unique_nodes(nodes):
    out = []
    seen = set()
    for node in nodes:
        if node is None:
            continue
        key = _node_key(node)
        if key in seen:
            continue
        seen.add(key)
        out.append(node)
    return out


def _get_graph_editor(blueprint, graph):
    cls = getattr(unreal, 'BlueprintGraphEditor', None)
    if cls is None:
        return None
    if graph is not None and hasattr(cls, 'get_graph_editor'):
        try:
            editor = cls.get_graph_editor(graph)
            if editor:
                return editor
        except Exception:
            pass
    if blueprint is not None and graph is not None and hasattr(cls, 'get_graph_editor_by_name'):
        name = graph.get_name()
        args = [name]
        if hasattr(unreal, 'Name'):
            try:
                args.append(unreal.Name(str(name)))
            except Exception:
                pass
        for arg in args:
            try:
                editor = cls.get_graph_editor_by_name(blueprint, arg)
                if editor:
                    return editor
            except Exception:
                continue
    return None


def _nodes_from_graph(blueprint, graph):
    info = {
        'blueprint': blueprint.get_path_name() if blueprint is not None else None,
        'graph': str(graph.get_name()) if graph is not None else None,
        'graph_path': graph.get_path_name() if graph is not None else None,
        'editor': False,
        'list_all_nodes': 0,
        'list_comment_nodes': 0,
        'reflected_Nodes': 0,
        'sample': [],
    }
    nodes = []
    reflected = _as_list(_try_uproperty(graph, ('Nodes', 'nodes')))
    info['reflected_Nodes'] = len(reflected)
    nodes.extend(reflected)
    if not nodes:
        editor = _get_graph_editor(blueprint, graph)
        if editor is not None:
            info['editor'] = True
            k2_cls = getattr(unreal, 'K2Node', None)
            comment_cls = getattr(unreal, 'EdGraphNode_Comment', None)
            listed = _invoke_out_array(editor, 'list_all_nodes', k2_cls)
            comments = _invoke_out_array(editor, 'list_comment_nodes', comment_cls)
            info['list_all_nodes'] = len(listed)
            info['list_comment_nodes'] = len(comments)
            nodes.extend(listed)
            nodes.extend(comments)
    info['sample'] = [_class_name(node) for node in nodes[:8]]
    return _unique_nodes(nodes), info


def _blueprint_from_class(cls):
    lib = unreal.BlueprintEditorLibrary
    if cls is None:
        return None
    if hasattr(lib, 'get_blueprint_for_class'):
        for call in (
            lambda: lib.get_blueprint_for_class(cls),
            lambda: lib.get_blueprint_for_class(cls, True),
            lambda: lib.get_blueprint_for_class(cls, False),
        ):
            try:
                result = call()
            except Exception:
                continue
            candidate = result[0] if isinstance(result, (tuple, list)) and result else result
            if candidate is not None and isinstance(candidate, unreal.Blueprint):
                return candidate
    if hasattr(lib, 'get_blueprint_asset'):
        try:
            candidate = lib.get_blueprint_asset(cls)
            if candidate:
                return candidate
        except Exception:
            pass
    return None


def _blueprint_parent_chain(blueprint):
    chain = []
    seen = set()
    current = blueprint
    while current is not None and len(chain) < 8:
        try:
            key = current.get_path_name()
        except Exception:
            break
        if key in seen:
            break
        seen.add(key)
        chain.append(current)
        parent_class = None
        lib = unreal.BlueprintEditorLibrary
        if hasattr(lib, 'get_blueprint_parent_class'):
            try:
                parent_class = lib.get_blueprint_parent_class(current)
            except Exception:
                parent_class = None
        current = _blueprint_from_class(parent_class)
    return chain


def _collect_graph_nodes(blueprint, graph, probe):
    probe['module'] = __file__
    probe['BlueprintGraphEditor'] = hasattr(unreal, 'BlueprintGraphEditor')
    attempts = []
    target_name = str(graph.get_name()) if graph is not None else ''
    for index, bp in enumerate(_blueprint_parent_chain(blueprint)):
        candidate = graph if index == 0 else _find_graph(bp, target_name)
        if candidate is None:
            continue
        nodes, info = _nodes_from_graph(bp, candidate)
        attempts.append(info)
        if nodes:
            probe['used'] = info
            probe['attempts'] = attempts
            return nodes
    # Never scan every EdGraphNode in the process: that AV'd UE 5.8 on
    # LevelButton (Export Project died at "collecting LevelButton").
    probe['attempts'] = attempts
    return []


def _graph_nodes(graph):
    raw = _try_uproperty(graph, ('Nodes', 'nodes'))
    items = _as_list(raw)
    if items:
        return items
    return []


def _write_probe_if_empty(dump, out_dir):
    if dump.get('nodes') or dump.get('comments'):
        return None
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    path = os.path.join(out_dir, '{}_{}.probe.json'.format(
        _safe_filename(dump.get('blueprint')),
        _safe_filename(dump.get('graph')),
    ))
    payload = {
        'blueprint': dump.get('blueprint_path'),
        'graph': dump.get('graph'),
        'raw_node_count': dump.get('raw_node_count'),
        'warnings': dump.get('warnings'),
        'probe': dump.get('probe'),
        'module': __file__,
    }
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=2, default=str)
        handle.write('\n')
    return path


def _blueprint_graphs(blueprint):
    graphs = []
    seen = set()
    for prop in (
        'UbergraphPages', 'ubergraph_pages',
        'FunctionGraphs', 'function_graphs',
        'MacroGraphs', 'macro_graphs',
        'DelegateSignatureGraphs', 'delegate_signature_graphs',
    ):
        for graph in _as_list(_try_uproperty(blueprint, (prop,))):
            try:
                key = graph.get_path_name()
            except Exception:
                key = id(graph)
            if key in seen:
                continue
            seen.add(key)
            graphs.append(graph)
    lib = unreal.BlueprintEditorLibrary
    if hasattr(lib, 'list_graphs'):
        for graph in _as_list(lib.list_graphs(blueprint)):
            try:
                key = graph.get_path_name()
            except Exception:
                key = id(graph)
            if key in seen:
                continue
            seen.add(key)
            graphs.append(graph)
    if hasattr(lib, 'find_event_graph'):
        event_graph = lib.find_event_graph(blueprint)
        if event_graph is not None:
            try:
                key = event_graph.get_path_name()
            except Exception:
                key = id(event_graph)
            if key not in seen:
                graphs.append(event_graph)
    return graphs


def _resolve_graph_with_nodes(blueprint, graph):
    if graph is not None and _graph_nodes(graph):
        return graph
    name = str(graph.get_name()) if graph is not None else ''
    for candidate in _blueprint_graphs(blueprint):
        if name and str(candidate.get_name()) != name:
            continue
        if _graph_nodes(candidate):
            return candidate
    return graph


def _find_graph(blueprint, graph_name):
    lib = unreal.BlueprintEditorLibrary
    name = str(graph_name)
    if hasattr(lib, 'find_graph'):
        graph = lib.find_graph(blueprint, name)
        if graph:
            return graph
    if name.lower() == 'eventgraph' and hasattr(lib, 'find_event_graph'):
        graph = lib.find_event_graph(blueprint)
        if graph:
            return graph
    for graph in _blueprint_graphs(blueprint):
        if str(graph.get_name()) == name:
            return graph
    return None


def _list_graph_names(blueprint):
    names = []
    seen = set()
    lib = unreal.BlueprintEditorLibrary
    if hasattr(lib, 'list_graph_names'):
        for name in _as_list(lib.list_graph_names(blueprint)):
            text = str(name)
            if text and text not in seen:
                seen.add(text)
                names.append(text)
    for graph in _blueprint_graphs(blueprint):
        text = str(graph.get_name())
        if text and text not in seen:
            seen.add(text)
            names.append(text)
    return names


def _load_any_asset(path):
    if unreal is None or not path:
        return None
    obj = unreal.load_asset(path)
    if obj is None:
        try:
            obj = unreal.EditorAssetLibrary.load_asset(path)
        except Exception:
            obj = None
    return obj


def _load_blueprint(path):
    obj = unreal.load_asset(path)
    if obj is None:
        try:
            obj = unreal.EditorAssetLibrary.load_asset(path)
        except Exception:
            obj = None
    if obj is None:
        return None
    if isinstance(obj, unreal.Blueprint):
        return obj
    lib = unreal.BlueprintEditorLibrary
    if hasattr(lib, 'get_blueprint_asset'):
        bp = lib.get_blueprint_asset(obj)
        if bp:
            return bp
    return None


def _blueprint_from_context(context):
    ctx_cls = getattr(unreal, 'BlueprintEditorToolMenuContext', None)
    if ctx_cls is not None:
        found = unreal.ToolMenus.find_context(context, ctx_cls)
        if found:
            bp = found.get_blueprint_obj()
            if bp:
                return bp
    return None


def _is_k2(node):
    k2 = getattr(unreal, 'K2Node', None)
    if k2 is not None and isinstance(node, k2):
        return True
    return _class_name(node).startswith('K2Node')


def _class_name(node):
    try:
        return str(node.get_class().get_name())
    except Exception:
        return type(node).__name__


def _node_key(node):
    try:
        return node.get_path_name()
    except Exception:
        return id(node)


def _node_title(node, lib):
    if hasattr(node, 'get_node_title'):
        try:
            return str(node.get_node_title() or '')
        except Exception:
            pass
    if hasattr(lib, 'get_node_title'):
        try:
            return str(lib.get_node_title(node) or '')
        except Exception:
            pass
    return node.get_name()


def _node_pos(node, lib):
    if hasattr(node, 'get_node_pos'):
        try:
            point = node.get_node_pos()
            return float(point.x), float(point.y)
        except Exception:
            pass
    if hasattr(lib, 'get_node_pos'):
        try:
            point = lib.get_node_pos(node)
            return float(point.x), float(point.y)
        except Exception:
            pass
    x = _try_uproperty(node, ('NodePosX', 'node_pos_x'))
    y = _try_uproperty(node, ('NodePosY', 'node_pos_y'))
    return float(x or 0), float(y or 0)


def _node_size(node, lib):
    if hasattr(lib, 'get_node_size'):
        try:
            size = lib.get_node_size(node)
            w, h = float(size.x), float(size.y)
            if w > 1 and h > 1:
                return w, h
        except Exception:
            pass
    return 0.0, 0.0


def _list_output_pins(node, lib):
    if hasattr(node, 'list_output_pins'):
        try:
            return list(node.list_output_pins() or [])
        except Exception:
            pass
    if hasattr(lib, 'list_output_pins'):
        try:
            return list(lib.list_output_pins(node) or [])
        except Exception:
            pass
    return [pin for pin in _list_all_pins(node, lib) if _pin_is_output(pin)]


def _list_input_pins(node, lib):
    if hasattr(node, 'list_input_pins'):
        try:
            return list(node.list_input_pins() or [])
        except Exception:
            pass
    if hasattr(lib, 'list_input_pins'):
        try:
            return list(lib.list_input_pins(node) or [])
        except Exception:
            pass
    return [pin for pin in _list_all_pins(node, lib) if not _pin_is_output(pin)]


def _list_all_pins(node, lib):
    if hasattr(node, 'list_all_pins'):
        try:
            return list(node.list_all_pins() or [])
        except Exception:
            pass
    if hasattr(lib, 'list_all_pins'):
        try:
            return list(lib.list_all_pins(node) or [])
        except Exception:
            pass
    return []


def _pin_valid(pin):
    if pin is None:
        return False
    if hasattr(pin, 'is_valid'):
        try:
            return bool(pin.is_valid())
        except Exception:
            return True
    return True


def _pin_name(pin):
    if hasattr(pin, 'get_pin_name'):
        try:
            return str(pin.get_pin_name())
        except Exception:
            pass
    return str(getattr(pin, 'pin_name', '') or '')


def _pin_value(pin):
    if not hasattr(pin, 'get_pin_value'):
        return ''
    try:
        raw = pin.get_pin_value()
    except Exception:
        return ''
    if raw is None or raw == '':
        return ''
    if isinstance(raw, str):
        return pretty_export_text(raw)
    return format_export_value(raw)


def _pin_owner(pin):
    if hasattr(pin, 'get_owning_node'):
        try:
            return pin.get_owning_node()
        except Exception:
            return None
    return None


def _pin_key(pin):
    owner = _pin_owner(pin)
    owner_key = _node_key(owner) if owner is not None else 'none'
    return owner_key + ':' + _pin_name(pin) + ':' + ('out' if _pin_is_output(pin) else 'in')


def _pin_is_output(pin):
    direction = None
    if hasattr(pin, 'get_pin_direction'):
        try:
            direction = pin.get_pin_direction()
        except Exception:
            direction = None
    if direction is None:
        return False
    output = getattr(unreal.EdGraphPinDirection, 'EGPD_OUTPUT', None)
    if output is not None:
        return direction == output
    return 'OUTPUT' in str(direction).upper()


def _pin_is_exec(pin):
    if hasattr(pin, 'get_pin_type'):
        try:
            pin_type = pin.get_pin_type()
            category = str(getattr(pin_type, 'pin_category', '') or '').lower()
            if category in EXEC_PIN_CATEGORIES or category.endswith('exec'):
                return True
        except Exception:
            pass
    return _pin_name(pin).lower() in EXEC_PIN_NAMES


def _connected_pins(pin):
    if hasattr(pin, 'list_connected_pins'):
        try:
            return list(pin.list_connected_pins() or [])
        except Exception:
            return []
    return []


def _pin_connected(pin):
    return bool(_connected_pins(pin))


def _as_list(raw):
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        count = len(raw)
        return [raw[index] for index in range(count)]
    except Exception:
        pass
    try:
        return list(raw)
    except TypeError:
        return [raw]


def _log(message):
    if unreal is not None:
        unreal.log(message)
    else:
        print(message)


def _log_error(message):
    if unreal is not None:
        unreal.log_error(message)
    else:
        print(message, file=sys.stderr)


def _copy_to_clipboard(text):
    if sys.platform != 'win32':
        return False
    try:
        import subprocess
        subprocess.run(['clip'], input=text.encode('utf-16'), check=False)
        return True
    except Exception:
        return False


def _notify(md_path, mermaid, probe_path=None):
    message = 'Wrote:\n{}\n\nMermaid copied to clipboard ({} chars).'.format(
        md_path, len(mermaid))
    if probe_path:
        message += '\n\nEmpty graph. Probe:\n{}'.format(probe_path)
    try:
        unreal.EditorDialog.show_message('Export Graph for LLM', message, unreal.AppMsgType.OK)
    except Exception:
        _log(message)


def _notify_folder(folder, message):
    try:
        unreal.EditorDialog.show_message('Export Graph for LLM', message, unreal.AppMsgType.OK)
    except Exception:
        _log(message)


def _python_toolbar_command(call_expr):
    return (
        'import os, sys, importlib, unreal; '
        'p = os.path.normpath(unreal.Paths.project_content_dir() + "Python/editor"); '
        'sys.path.remove(p) if p in sys.path else None; '
        'sys.path.insert(0, p); '
        'import bp2mermaid as _bp; '
        '_bp = importlib.reload(_bp); '
        + call_expr
    )


def _hooks():
    """Editor-process state that survives `importlib.reload` of this module."""
    state = getattr(sys, _HOOKS_ATTR, None)
    if state is None:
        state = {
            'registry_bound': False,
            'tick_handle': None,
            'dirty_names': set(),
            'exporting': False,
            'last_export_at': {},
            'toggle': None,
            'callback': None,
            'poll': None,
            'next_poll': 0.0,
        }
        setattr(sys, _HOOKS_ATTR, state)
    return state


def _live_mod():
    return sys.modules.get('bp2mermaid') or sys.modules.get(__name__)


def _ue_text(text):
    ctor = getattr(unreal, 'Text', None)
    if ctor is None:
        return text
    try:
        return ctor(text)
    except Exception:
        return text


def _check_box_state(enabled):
    enum = getattr(unreal, 'CheckBoxState', None)
    if enum is None:
        return enabled
    if enabled:
        return getattr(enum, 'CHECKED', getattr(enum, 'Checked', True))
    return getattr(enum, 'UNCHECKED', getattr(enum, 'Unchecked', False))


def _check_action_type():
    enum = getattr(unreal, 'UserInterfaceActionType', None)
    if enum is None:
        return None
    return getattr(enum, 'CHECK', getattr(enum, 'TOGGLE_BUTTON', None))


def _handle_asset_updated_on_disk(*args):
    if not is_auto_export_enabled():
        return
    asset_data = args[0] if args else None
    asset = _asset_from_asset_data(asset_data)
    name = ''
    if asset is not None:
        getter = getattr(asset, 'get_name', None)
        if callable(getter):
            try:
                name = getter()
            except Exception:
                name = ''
    if not name and asset_data is not None:
        name = str(getattr(asset_data, 'asset_name', '') or '')
        if not name:
            package_name = getattr(asset_data, 'package_name', None)
            if package_name:
                name = str(package_name)
    if is_data_asset_export_name(name):
        if asset is None:
            return
        auto_export_data_asset(asset)
        return
    blueprint = _as_blueprint(asset)
    if blueprint is None:
        return
    auto_export_blueprint(blueprint)


def _asset_from_asset_data(asset_data):
    if asset_data is None:
        return None
    asset = None
    getter = getattr(asset_data, 'get_asset', None)
    if getter is not None:
        try:
            asset = getter()
        except Exception:
            asset = None
    if asset is None:
        package_name = getattr(asset_data, 'package_name', None)
        if package_name:
            asset = _load_any_asset(str(package_name))
    return asset


def _blueprint_from_asset_data(asset_data):
    return _as_blueprint(_asset_from_asset_data(asset_data))


def _as_blueprint(obj):
    if obj is None or unreal is None:
        return None
    blueprint = None
    if isinstance(obj, unreal.Blueprint):
        blueprint = obj
    else:
        lib = unreal.BlueprintEditorLibrary
        if hasattr(lib, 'get_blueprint_asset'):
            try:
                blueprint = lib.get_blueprint_asset(obj)
            except Exception:
                blueprint = None
    if blueprint is None:
        return None
    try:
        name = blueprint.get_name()
    except Exception:
        name = ''
    if name.startswith('SKEL_') or name.startswith('REINST_'):
        return None
    return blueprint


def _dirty_package_names():
    utils = getattr(unreal, 'EditorLoadingAndSavingUtils', None)
    if utils is None:
        return set()
    packages = _invoke_out_array(
        utils, 'get_dirty_content_packages', getattr(unreal, 'Package', None))
    names = set()
    for package in packages:
        try:
            names.add(str(package.get_path_name()))
        except Exception:
            continue
    return names


def _auto_export_package_path(package_path):
    name = package_path.rsplit('/', 1)[-1]
    if is_data_asset_export_name(name):
        obj = _load_any_asset(package_path)
        if obj is None:
            obj = _load_any_asset('{}.{}'.format(package_path, name))
        if obj is None:
            return None
        return auto_export_data_asset(obj)
    blueprint = _load_blueprint(package_path)
    if blueprint is None:
        blueprint = _load_blueprint('{}.{}'.format(package_path, name))
    if blueprint is None:
        return None
    return auto_export_blueprint(blueprint)


def _poll_dirty_packages():
    if not is_auto_export_enabled():
        _stop_dirty_poll()
        return
    hooks = _hooks()
    now = time.time()
    if now < hooks.get('next_poll', 0.0):
        return
    hooks['next_poll'] = now + AUTO_EXPORT_POLL_SEC
    current = _dirty_package_names()
    previous = hooks.get('dirty_names') or set()
    hooks['dirty_names'] = current
    for path in previous - current:
        _auto_export_package_path(path)


def _try_bind_asset_registry():
    hooks = _hooks()
    if hooks.get('registry_bound'):
        return True
    helpers = getattr(unreal, 'AssetRegistryHelpers', None)
    if helpers is None or not hasattr(helpers, 'get_asset_registry'):
        return False
    try:
        registry = helpers.get_asset_registry()
    except Exception:
        return False
    callback = hooks.get('callback')
    if callback is None:
        def callback(*args):
            mod = _live_mod()
            if mod is not None:
                mod._handle_asset_updated_on_disk(*args)
        hooks['callback'] = callback
    for attr in ('on_asset_updated_on_disk', 'on_asset_updated'):
        delegate = getattr(registry, attr, None)
        if delegate is None:
            continue
        if callable(delegate) and not hasattr(delegate, 'add_callable'):
            try:
                delegate = delegate()
            except Exception:
                continue
        adder = getattr(delegate, 'add_callable_unique', None) or getattr(delegate, 'add_callable', None)
        if adder is None:
            continue
        adder(callback)
        hooks['registry_bound'] = True
        _log('bp2mermaid: auto-export listening on AssetRegistry.{}'.format(attr))
        return True
    return False


def _start_dirty_poll():
    hooks = _hooks()
    if hooks.get('tick_handle') is not None:
        return
    register = getattr(unreal, 'register_slate_post_tick_callback', None)
    if register is None:
        _log_error('bp2mermaid: no AssetRegistry save delegate and no slate tick callback')
        return
    hooks['dirty_names'] = _dirty_package_names()
    hooks['next_poll'] = time.time()

    def tick(_delta):
        mod = _live_mod()
        if mod is not None:
            mod._poll_dirty_packages()

    hooks['poll'] = tick
    hooks['tick_handle'] = register(tick)
    _log('bp2mermaid: auto-export watching dirty packages (slate tick)')


def _stop_dirty_poll():
    hooks = _hooks()
    handle = hooks.get('tick_handle')
    if handle is None:
        return
    unregister = getattr(unreal, 'unregister_slate_post_tick_callback', None)
    if unregister is not None:
        try:
            unregister(handle)
        except Exception:
            pass
    hooks['tick_handle'] = None


def _sync_save_watch():
    if unreal is None:
        return
    hooks = _hooks()
    if hooks.get('registry_bound'):
        _stop_dirty_poll()
        return
    if is_auto_export_enabled():
        _start_dirty_poll()
    else:
        _stop_dirty_poll()


def _ensure_save_watch():
    if unreal is None:
        return
    _try_bind_asset_registry()
    _sync_save_watch()


def _configure_toggle_script(script, menu):
    menu_name = getattr(menu, 'menu_name', None) or unreal.Name('')
    script.init_entry(
        unreal.Name('Bp2Mermaid'),
        menu_name,
        unreal.Name('Graphs'),
        unreal.Name('EnableAutoExport'),
        _ue_text('Enable auto-export'),
        _ue_text('When enabled, this Blueprint is re-exported automatically on save'),
    )
    ui_type = _check_action_type()
    advanced = getattr(getattr(script, 'data', None), 'advanced', None)
    if advanced is None:
        return
    if ui_type is not None:
        try:
            advanced.user_interface_action_type = ui_type
        except Exception:
            pass
    for attr in (
        'should_close_window_after_menu_selection',
        'b_should_close_window_after_menu_selection',
    ):
        if hasattr(advanced, attr):
            try:
                setattr(advanced, attr, False)
            except Exception:
                pass
            break


def _add_separator(menu, name):
    sep_type = getattr(unreal.MultiBlockType, 'SEPARATOR', None)
    if sep_type is None:
        return
    sep = unreal.ToolMenuEntry(name=name, type=sep_type)
    menu.add_menu_entry('Graphs', sep)


def _add_command_entry(menu, name, label, tooltip, call_expr):
    entry = unreal.ToolMenuEntry(name=name, type=unreal.MultiBlockType.MENU_ENTRY)
    entry.set_label(label)
    entry.set_tool_tip(tooltip)
    entry.set_string_command(
        unreal.ToolMenuStringCommandType.PYTHON,
        unreal.Name(''),
        _python_toolbar_command(call_expr),
    )
    menu.add_menu_entry('Graphs', entry)


def _add_export_project_entry(menu):
    name = project_display_name()
    _add_command_entry(
        menu,
        'ExportProject',
        'Export {}'.format(name),
        'Export every /Game Blueprint plus GA_/GE_/DA_ assets into the Content-mirrored markdown tree (skip unchanged)',
        '_bp.export_project()',
    )


def _add_auto_export_toggle(menu):
    sep_type = getattr(unreal.MultiBlockType, 'SEPARATOR', None)
    if sep_type is not None:
        sep = unreal.ToolMenuEntry(name='AutoExportSep', type=sep_type)
        menu.add_menu_entry('Graphs', sep)
    hooks = _hooks()
    toggle = hooks.get('toggle')
    if toggle is None or type(toggle).__name__ != 'Bp2MermaidAutoExportToggle':
        toggle = Bp2MermaidAutoExportToggle()
        hooks['toggle'] = toggle
        _RETAIN.append(toggle)
    _configure_toggle_script(toggle, menu)
    try:
        menu.add_menu_entry_object(toggle)
        return
    except Exception as error:
        _log_error('bp2mermaid: failed to add auto-export checkbox: {}'.format(error))
    entry = unreal.ToolMenuEntry(
        name='EnableAutoExport',
        type=unreal.MultiBlockType.MENU_ENTRY,
    )
    entry.set_label('Enable auto-export ({})'.format(
        'ON' if is_auto_export_enabled() else 'OFF'))
    entry.set_tool_tip('When enabled, this Blueprint is re-exported automatically on save')
    entry.set_string_command(
        unreal.ToolMenuStringCommandType.PYTHON,
        unreal.Name(''),
        _python_toolbar_command('_bp.toggle_auto_export()'),
    )
    menu.add_menu_entry('Graphs', entry)


def register_menu():
    """Add the Export Graph combo button to the Blueprint Editor toolbar."""
    if unreal is None:
        return False
    try:
        _register_menu_impl()
        _ensure_save_watch()
        _log('bp2mermaid: registered on Blueprint Editor toolbar ({})'.format(__file__))
        return True
    except Exception as error:
        _log_error('bp2mermaid: register_menu failed: {}'.format(error))
        return False


def _register_menu_impl():
    menus = unreal.ToolMenus.get()
    toolbar_name = 'AssetEditor.BlueprintEditor.ToolBar'
    entry_name = 'Bp2MermaidExport'
    section_name = 'Bp2MermaidExport'
    submenu_name = toolbar_name + '.' + entry_name

    toolbar = menus.extend_menu(toolbar_name)
    toolbar.add_section(section_name, 'LLM')

    combo = getattr(unreal.MultiBlockType, 'TOOL_BAR_COMBO_BUTTON', unreal.MultiBlockType.MENU_ENTRY)
    entry = unreal.ToolMenuEntry(name=entry_name, type=combo)
    entry.set_label('Export Graph')
    entry.set_tool_tip('Export this Blueprint to Mermaid for LLM and git')
    toolbar.add_menu_entry(section_name, entry)

    submenu = menus.register_menu(submenu_name, '', unreal.MultiBoxType.MENU, False)
    if submenu is None:
        submenu = menus.extend_menu(submenu_name)
    section_object = Bp2MermaidSection()
    _RETAIN.append(section_object)
    submenu.add_dynamic_section('Graphs', section_object)
    menus.refresh_all_widgets()


if unreal is not None:
    @unreal.uclass()
    class Bp2MermaidSection(unreal.ToolMenuSectionDynamic):
        @unreal.ufunction(override=True)
        def construct_sections(self, menu, context):
            menu.add_section('Graphs', 'Graphs')
            blueprint = _blueprint_from_context(context)
            if blueprint is not None:
                _add_command_entry(
                    menu,
                    'ExportAll',
                    'Export {}'.format(blueprint.get_name()),
                    'Export every graph of this Blueprint into the Content-mirrored markdown tree',
                    '_bp.export_all_by_path({!r})'.format(blueprint.get_path_name()),
                )
            _add_export_project_entry(menu)
            _add_auto_export_toggle(menu)

    @unreal.uclass()
    class Bp2MermaidAutoExportToggle(unreal.ToolMenuEntryScript):
        @unreal.ufunction(override=True)
        def execute(self, context):
            enabled = toggle_auto_export()
            _log('bp2mermaid: auto-export {}'.format('ON' if enabled else 'OFF'))

        @unreal.ufunction(override=True)
        def get_check_state(self, context):
            return _check_box_state(is_auto_export_enabled())


if __name__ == '__main__':
    if unreal is None:
        raise SystemExit('Run this script inside Unreal Editor (py bp2mermaid.py)')
    register_menu()
    report = run_editor_selftest(notify=False)
    unreal.log('Export Graph selftest report: {}'.format(report.get('report_path')))
    if report.get('errors'):
        unreal.log_warning('Export Graph selftest errors: {}'.format(report['errors']))
