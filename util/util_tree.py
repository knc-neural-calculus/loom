import uuid
import html2text
import numpy as np
import random


# Height of d, root has the greatest height, minimum is 1
def height(d):
    return 1 + max([0, *[height(c) for c in d["children"]]])

# Depth of d, root is 0 depth
def depth(d, node_dict):
    return 0 if "parent_id" not in d else (1 + depth(node_dict[d["parent_id"]], node_dict))


# Returns a list of ancestor nodes beginning with the progenitor
def node_ancestry(node, node_dict):
    ancestry = [node]
    while "parent_id" in node:
        node = node_dict[node["parent_id"]]
        ancestry.insert(0, node)
    return ancestry


# recursively called on subtree
def overwrite_subtree(node, attribute, new_value, old_value=None, force_overwrite=False):
    if force_overwrite or (attribute not in node) or old_value is None or (node[attribute] == old_value) \
            or (node[attribute] == new_value):
        node[attribute] = new_value
        terminal_nodes_list = []
        for child in node['children']:
            terminal_nodes_list += overwrite_subtree(child, attribute, new_value, old_value, force_overwrite)
        return terminal_nodes_list
    else:
        return [node]


def stochastic_transition(node, mode='descendents'):
    transition_probs = subtree_weights(node, mode)
    choice = random.choices(node['children'], transition_probs, k=1)
    return choice[0]


def subtree_weights(node, mode='descendents'):
    weights = []
    if 'children' in node:
        for child in node['children']:
            if mode == 'descendents':
                weights.append(num_descendents(child))
            elif mode == 'leaves':
                weights.append(num_leaves(child))
            elif mode == 'uniform':
                weights.append(1)
            else:
                print('invalid mode for subtree weights')
    #print('unnormalized probabilities: ', weights)
    norm = np.linalg.norm(weights, ord=1)
    normalized_weights = weights / norm
    #print('probabilities: ', normalized_weights)
    return normalized_weights


def num_descendents(node):
    descendents = 1
    if 'children' in node:
        for child in node['children']:
            descendents += num_descendents(child)
    return descendents


def num_leaves(node):
    if 'children' in node and len(node['children']) > 0:
        leaves = 0
        for child in node['children']:
            leaves += num_descendents(child)
        return leaves
    else:
        return 1

# {
#   root: {
#       text: ...
#       children: [
#           {
#               text: ...
#               children: ...
#           },
#       ]
#   }
#   generation_settings: {...}
# }
# Adds an ID field and a parent ID field to each dict in a recursive tree with "children"
def flatten_tree(d):
    if "id" not in d:
        d["id"] = str(uuid.uuid1())

    children = d.get("children", [])
    flat_children = []
    for child in children:
        child["parent_id"] = d["id"]
        flat_children.extend(flatten_tree(child))

    return [d, *flat_children]


def flatten_tree_revisit_parents(d, parent=None):
    if "id" not in d:
        d["id"] = str(uuid.uuid1())

    children = d.get("children", [])
    flat_children = []
    for child in children:
        child["parent_id"] = d["id"]
        flat_children.extend(flatten_tree_revisit_parents(child, d))

    return [d, *flat_children] if parent is None else [d, *flat_children, parent]


# Remove html and random double newlines from Miro
def fix_miro_tree(flat_data):
    # Otherwise it will randomly insert line breaks....
    h = html2text.HTML2Text()
    h.body_width = 0

    id_to_node = {d["id"]: d for d in flat_data}
    for d in flat_data:
        # Only fix miro text
        if "text" not in d or all([tag not in d["text"] for tag in ["<p>", "</p"]]):
            continue

        d["text"] = h.handle(d["text"])

        # p tags lead to double newlines
        d["text"] = d["text"].replace("\n\n", "\n")

        # Remove single leading and trailing newlines added by p tag wrappers
        if d["text"].startswith("\n"):
            d["text"] = d["text"][1:]
        if d["text"].endswith("\n"):
            d["text"] = d["text"][:-1]

        # No ending spaces, messes with generation
        d["text"] = d["text"].rstrip(" ")

        # If the text and its parent starts without a new line, it needs a space:
        if not d["text"].startswith("\n") and \
                ("parent_id" not in d or not id_to_node[d["parent_id"]]["text"].endswith("\n")):
            d["text"] = " " + d["text"]


