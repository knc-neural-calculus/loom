import functools
import os
import threading
import time
import uuid
from pprint import pprint

import numpy as np
from collections import defaultdict, ChainMap
from multiprocessing.pool import ThreadPool

from gpt import api_generate, janus_generate
from util.util import json_create, timestamp, json_open, clip_num, index_clip
from util.util_tree import fix_miro_tree, flatten_tree, node_ancestry, overwrite_subtree


# Calls any callbacks associated with the wrapped function
# class must have a defaultdict(list)[func_name] = [*callbacks]
# https://stackoverflow.com/questions/11731136/class-method-decorator-with-self-arguments
# TODO Flag to skip callbacks
def event(func):
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        output = func(self, *args, **kwargs)
        [callback() for callback in self.callbacks[func.__name__]]
        return output

    return wrapper

# The old way.
# # Run callbacks for the method that calls this
# # This is VERY slow because of inspect.stack()
# # put inside class!
# def _run_callbacks(self):
#     callers_name = inspect.stack()[1][3]
#     print(callers_name)
#     [callback() for callback in self.callbacks[callers_name]]


DEFAULT_GENERATION_SETTINGS = {
    'num_continuations': 4,
    'temperature': 0.9,
    'top_p': 1,
    'response_length': 100,
    'prompt_length': 6000,
    "janus": False,
    "adaptive": False,
    "model": "davinci",
    "memory": "",
}

DEFAULT_VISUALIZATION_SETTINGS = {
    'textwidth': 450,
    'leafdist': 200,
    'leveldistance': 150,
    'textsize': 10,
    'horizontal': True,
    'displaytext': True,
    'showbuttons': True,
}

EMPTY_TREE = {
    "root": {
        "text": "",
        "children": [],
    },
    "chapters": {}
}

class TreeModel:

    def __init__(self, root):
        self.app = root
        self.app.bind("<<TreeUpdated>>", lambda _: self.tree_updated())

        # All variables initialized below
        self.tree_filename = None
        # tree with all data
        self.tree_raw_data = None
        # CALCULATED {node_id: node}
        self.tree_node_dict = None
        # {chapter_id: chapter}
        self.chapters = None
        self.checkpoint = None

        self.selected_node_id = None

        self.callbacks = defaultdict(list)


    @property
    def visualization_settings(self):
        return self.tree_raw_data.get("visualization_settings") \
            if self.tree_raw_data and "visualization_settings" in self.tree_raw_data \
            else DEFAULT_VISUALIZATION_SETTINGS

    @property
    def generation_settings(self):
        return self.tree_raw_data.get("generation_settings") \
            if self.tree_raw_data and "generation_settings" in self.tree_raw_data \
            else DEFAULT_GENERATION_SETTINGS


    #################################
    #   Hooks
    #################################

    def register_callback(self, func, callback):
        self.callbacks[func.__name__].append(callback)

    # Decorator calls callbacks
    @event
    def tree_updated(self, rebuild_dict=True):
        if self.tree_raw_data and rebuild_dict:
            self.tree_node_dict = {d["id"]: d for d in flatten_tree(self.tree_raw_data["root"])}
            fix_miro_tree(self.nodes)

    @event
    def pre_selection_updated(self):
        pass

    @event
    def selection_updated(self):
        pass

    @event
    def io_update(self):
        pass

    #################################
    #   Access
    #################################

    def node(self, node_id=None):
        if node_id is None:
            return self.selected_node
        return self.tree_node_dict[node_id] if self.tree_node_dict and node_id in self.tree_node_dict else None

    def parent(self, node):
        return self.tree_node_dict[node['parent_id']]

    # Get a nodes chapter by finding its chapter or its nearest parent's chapter
    def chapter(self, node):
        for lineage_node in reversed(node_ancestry(node, self.tree_node_dict)):
            if "chapter_id" in lineage_node:
                return self.chapters[lineage_node["chapter_id"]]
        return None

    def node_ancestry_text(self, node=None):
        node = node if node else self.selected_node
        return [node["text"] for node in node_ancestry(node, self.tree_node_dict)]

    @property
    def selected_node(self):
        if self.tree_node_dict is None or self.selected_node_id not in self.tree_node_dict:
            return None
        # if self.selected_node_id is None or self.selected_node_id not in self.tree_node_dict:
        #     self.select_node(self.nodes[0]["id"])
        return self.tree_node_dict[self.selected_node_id]

    @property
    def selected_chapter(self):
        return self.chapter(self.selected_node) if self.selected_node is not None else None

    @property
    def nodes(self):
        return list(self.tree_node_dict.values()) if self.tree_node_dict else None

    @property
    def tree_traversal_idx(self):
        return self.nodes.index(self.selected_node)

    # Returns [{chapter: {}, id, children: []}, ...]
    def _build_chapter_trees(self, node):
        # Returns a 1 element list if the node is a chapter, else a list of children chapters
        children_chapter_lists = [self._build_chapter_trees(child) for child in node["children"]]
        children_chapters = [item for sublist in children_chapter_lists for item in sublist]
        if "chapter_id" in node:
            chapter = self.chapters[node["chapter_id"]]
            return [{
                "chapter": chapter,
                "id": chapter["id"],
                "children": children_chapters
            }]
        else:
            return children_chapters

    # Returns tuple of
    #  [ {chapter{}, id, parent_id, children[]}, ... ]
    #  {chapter_id: {chapter: {id:1, title:""}, id:1, parent_id, children[]}]
    def build_chapter_trees(self):
        node = self.tree_raw_data["root"]
        chapter_trees = self._build_chapter_trees(node)
        flat_trees = [flatten_tree(chapter_tree) for chapter_tree in chapter_trees]
        flat_maps = [{d["id"]: d for d in flat_tree} for flat_tree in flat_trees]
        chapter_tree_nodes = dict(ChainMap(*flat_maps))
        return chapter_trees, chapter_tree_nodes

    #################################
    #   Traversal
    #################################

    # Update the selected node, the nav tree selection, and possibly the position in the tree traversal
    def select_node(self, node_id, fire_callbacks=True):
        if self.selected_node_id != node_id and self.tree_node_dict and node_id in self.tree_node_dict:
            self.pre_selection_updated()

            self.selected_node_id = node_id
            self.selected_node["visited"] = True
            self.tree_raw_data["selected_node_id"] = self.selected_node_id

            # Open all parents but not the node itself
            ancestors = node_ancestry(self.selected_node, self.tree_node_dict)
            for ancestor in ancestors[:-1]:
                ancestor["open"] = True
            # Always open the root
            self.tree_raw_data["root"]["open"] = True

            if fire_callbacks:
                self.selection_updated()
            return self.selected_node

    def traverse_tree(self, offset):
        if self.tree_node_dict:
            new_idx = clip_num(self.tree_traversal_idx + offset, 0, len(self.tree_node_dict) - 1)
            new_node_id = self.nodes[new_idx]["id"]
            return self.select_node(new_node_id)

    def select_parent(self, node=None):
        node = node if node else self.selected_node
        if node and "parent_id" in node:
            return self.select_node(node["parent_id"])

    # Clips index
    def select_child(self, child_num, node=None):
        node = node if node else self.selected_node
        if node and len(node["children"]) > 0:
            return self.select_node(index_clip(node["children"], child_num)["id"])

    # Repeats siblings
    def select_sibling(self, offset, node=None):
        node = node if node else self.selected_node
        if node and "parent_id" in node:
            siblings = self.tree_node_dict[node["parent_id"]]["children"]
            sibling = siblings[(siblings.index(node) + offset) % len(siblings)]
            return self.select_node(sibling["id"])


    #################################
    #   Updates
    #################################

    def create_child(self, parent=None, update_selection=True, expand=True):
        parent = parent if parent else self.selected_node
        if not parent:
            return

        new_child = {"id": str(uuid.uuid1()),
                     "text": "",
                     "children": []}
        parent["children"].append(new_child)
        self.tree_updated()

        if update_selection:
            self.select_node(new_child["id"])
        if expand:
            new_child["open"] = True

        return new_child

    def create_sibling(self, node=None, update_selection=True):
        node = node if node else self.selected_node
        if not node:
            return
        parent = self.tree_node_dict[node["parent_id"]]
        self.create_child(parent=parent, update_selection=update_selection)

    def create_parent(self, node=None):
        node = node if node else self.selected_node
        if not node:
            return

        new_parent = {
            "id": str(uuid.uuid1()),
            "text": "",
            "children": [node]
        }
        if "parent_id" not in node:
            assert self.tree_raw_data["root"] == node
            self.tree_raw_data["root"] = new_parent
        else:
            old_siblings = self.tree_node_dict[node["parent_id"]]["children"]
            old_siblings[old_siblings.index(node)] = new_parent
            new_parent["parent_id"] = node["parent_id"]
        node["parent_id"] = new_parent["id"]

        self.tree_updated()

    def merge_with_parent(self, node=None):
        node = node if node else self.selected_node
        if not node:
            return

        parent = self.tree_node_dict[node["parent_id"]]
        parent["text"] += node["text"]

        index_in_parent = parent["children"].index(node)
        parent["children"][index_in_parent:index_in_parent + 1] = node["children"]
        for i, c in enumerate(node["children"]):
            # parent["children"].insert(index_in_parent+i, c)
            c["parent_id"] = parent["id"]

        self.select_node(parent["id"])
        self.tree_updated()

    def merge_with_children(self, node=None):
        node = node if node else self.selected_node
        if not node:
            return

        children = node["children"]
        for child in children:
            child["text"] = node["text"] + child["text"]
        self.delete_node(node, reassign_children=True)

    # TODO check if ancestor
    # TODO indicate that change parent has been toggled
    def change_parent(self, node=None, new_parent_id=None):
        node = node if node else self.selected_node
        if not node:
            return

        if node["id"] == new_parent_id:
            return
        if "parent_id" not in node:
            assert self.tree_raw_data["root"] == node
            print('ERROR: node is root')
            return
        elif new_parent_id == node["parent_id"]:
            return
        old_siblings = self.tree_node_dict[node["parent_id"]]["children"]
        old_siblings.remove(node)
        node["parent_id"] = new_parent_id
        self.tree_node_dict[new_parent_id]["children"].append(node)
        self.tree_updated()

    def add_parent(self, node=None, new_ghostparent=None):
        pass

    def change_main_parent(self, node=None, new_main_parent=None):
        pass

    # TODO Doesn't support deleting root
    def delete_node(self, node=None, reassign_children=False):
        node = node if node else self.selected_node
        if "parent_id" not in node:
            return

        parent = self.tree_node_dict[node["parent_id"]]
        siblings = parent["children"]
        old_index = siblings.index(node)
        siblings.remove(node)
        if reassign_children:
            siblings.extend(node["children"])

        # Select parent or the next sibling if possible and not keeping the children
        if reassign_children or len(siblings) == 0:
            self.select_node(parent["id"])
        else:
            self.select_node(siblings[old_index % len(siblings)]["id"])
        self.tree_updated()

    def update_text(self, node, text, active_text=None):
        assert node["id"] in self.tree_node_dict, text

        # Remove trailing spaces
        # count spaces that will be removedbb
        num_spaces = 0
        while text.endswith(" "):
            num_spaces += 1
            text = text[:-1]

        edited = False
        if node["text"] != text:
            # Give children spaces removed from text
            for child in node["children"]:
                child["text"] = " " * num_spaces + child["text"]
            node["text"] = text
            edited = True

        if active_text is not None and node.get("active_text", "") != active_text:
            node["active_text"] = active_text
            edited = True

        if edited:
            self.tree_updated()

    #################################
    #   Chapters
    #################################

    def import_chapters(self, root, chapters):
        if 'chapter_id' in root and root['chapter_id'] not in self.chapters:
            self.chapters[root['chapter_id']] = chapters[root['chapter_id']]
        for child in root['children']:
            self.import_chapters(child, chapters)

    def chapter_title(self, node):
        return self.chapters[node['chapter_id']]['title'] if "chapter_id" in node else ""

    def create_new_chapter(self, node, title):
        if "chapter_id" in node:
            self.delete_chapter(self.chapters[node["chapter_id"]], update_tree=False)
        if title:
            new_chapter = {
                "id": str(uuid.uuid1()),
                "root_id": node["id"],
                "title": title,
            }
            self.chapters[new_chapter["id"]] = new_chapter
            node["chapter_id"] = new_chapter["id"]
        self.tree_updated()

    def delete_chapter(self, chapter, update_tree=True):
        self.chapters.pop(chapter["id"])
        self.tree_node_dict[chapter["root_id"]].pop("chapter_id")
        if update_tree:
            self.tree_updated()

    def remove_all_chapters(self, node=None):
        was_root = node is None
        node = node if node else self.tree_raw_data['root']
        if "chapter_id" in node:
            self.delete_chapter(self.chapters[node["chapter_id"]], update_tree=False)
        for child in node["children"]:
            self.remove_all_chapters(child)
        if was_root:
            self.tree_updated()


    #################################
    #   I/O
    #################################

    # Inits empty chapters, memory, and notes if not already in tree
    def _init_global_objects(self):
        # Chapters
        if 'chapters' not in self.tree_raw_data:
            self.tree_raw_data['chapters'] = {}
        self.chapters = self.tree_raw_data["chapters"]

        # Generation settings
        self.tree_raw_data["generation_settings"] = {
            **DEFAULT_GENERATION_SETTINGS.copy(),
            **self.tree_raw_data.get("generation_settings", {})
        }

        # View settings # TODO If there are more of these, reduce duplication
        self.tree_raw_data["visualization_settings"] = {
            **DEFAULT_VISUALIZATION_SETTINGS.copy(),
            **self.tree_raw_data.get("visualization_settings", {})
        }
        # Accidentally added generation settings to this dict once. Remove them
        # FIXME remove when this is no longer a problem
        for key in DEFAULT_GENERATION_SETTINGS.keys():
            if key not in DEFAULT_VISUALIZATION_SETTINGS:
                self.tree_raw_data["visualization_settings"].pop(key, None)


    def load_tree_data(self, data):
        self.tree_raw_data = data

        if "root" not in self.tree_raw_data:
            assert "text" in self.tree_raw_data
            self.tree_raw_data = {
                "root": self.tree_raw_data
            }
        self.tree_node_dict = {d["id"]: d for d in flatten_tree(self.tree_raw_data["root"])}

        # If things don't have an open state, give one to them
        for node in self.tree_node_dict.values():
            node["open"] = node.get("open", False)

        self._init_global_objects()
        self.tree_updated()
        self.select_node(self.tree_raw_data.get("selected_node_id", self.nodes[0]["id"]))

    # Open a new tree json
    def open_tree(self, filename):
        self.tree_filename = os.path.abspath(filename)
        self.load_tree_data(json_open(self.tree_filename))
        self.io_update()

    # Open a new tree json
    def import_tree(self, filename):
        self.tree_filename = os.path.abspath(filename)
        tree_json = json_open(self.tree_filename)
        if 'root' in tree_json:
            new_subtree_root = tree_json['root']
            self.selected_node['children'].append(new_subtree_root)
            new_subtree_root['parent_id'] = self.selected_node_id
            if 'chapters' in tree_json:
                self.import_chapters(new_subtree_root, tree_json['chapters'])
            self.tree_updated()
        else:
            print('improperly formatted tree')



    # Tree flat data is just a different view to tree raw data!
    # We edit tree flat data with tkinter and save raw data which is still in json form
    def save_tree(self, backup=True):
        if not self.tree_filename:
            return False

        # Fancy platform independent os.path
        filename = os.path.splitext(os.path.basename(self.tree_filename))[0]
        save_dir = os.path.dirname(self.tree_filename)
        backup_dir = os.path.join(save_dir, "backups")

        # Make backup before overwriting tree
        if backup and os.path.isfile(self.tree_filename):
            if not os.path.exists(backup_dir):
                os.mkdir(backup_dir)
            os.rename(self.tree_filename, os.path.join(backup_dir, f"{filename}-{timestamp()}.json"))

        # Save tree
        json_create(self.tree_filename, self.tree_raw_data)
        self.io_update()
        return True


    #################################
    #   Generation
    #################################

    def generate_for_nodes(self, prompt, nodes, grandchildren=None):
        # TODO memory
        if self.generation_settings['janus']:
            pool = ThreadPool(len(nodes))
            janus_responses = pool.map(janus_generate, [prompt] * len(nodes))
            results, errors = zip(*janus_responses)
            errors = [e for e in errors if e]
            error = errors[0] if errors else None
        else:
            results, error = api_generate(prompt=prompt,
                                          length=self.generation_settings['response_length'],
                                          num_continuations=len(nodes),
                                          temperature=self.generation_settings['temperature'],
                                          top_p=self.generation_settings['top_p'],
                                          engine=self.generation_settings['model'])
        if not error:
            pprint(self.generation_settings)
            if self.generation_settings['adaptive']:
                for i, result in enumerate(results.choices):
                    min_logprob = np.argmin(result["logprobs"]["token_logprobs"])
                    split_position = result["logprobs"]["text_offset"][min_logprob] - len(prompt)
                    childtext = result["text"][:split_position]
                    grandchild_text = result["text"][split_position:]
                    nodes[i]["text"] = childtext
                    grandchildren[i]["text"] = grandchild_text

            else:
                for index, node in enumerate(nodes):
                    node["text"] = results.choices[index]["text"]

        else:
            print("ERROR. Deleting failures")
            for node in nodes:
                node["text"] = "ERROR: " + error
                # Just delete instead
                parent = self.tree_node_dict[node["parent_id"]]
                parent["children"].remove(node)

        for result in results.choices:
            print("Generated continuation:\n", result['text'], "\nerror", error)

        # DO NOT CALL FROM THREAD: self.tree_updated()
        self.app.event_generate("<<TreeUpdated>>", when="tail")

    def generate_continuation(self, node=None, update_selection=False):
        node = node if node else self.selected_node
        if not node:
            return

        children = []
        grandchildren = []
        pprint(self.generation_settings)
        for i in range(self.generation_settings['num_continuations']):
            child = self.create_child(node, update_selection=False, expand=True)
            children.append(child)
            if self.generation_settings['adaptive']:
                grandchildren.append(self.create_child(child, update_selection=False, expand=True))

        prompt = "".join(self.node_ancestry_text(children[0]))
        prompt = prompt[-self.generation_settings['prompt_length']:]
        memory = self.generation_settings['memory']
        print("Memory:\n", memory)
        print("Prompt:\n", prompt[:100] + " ... " + prompt[-100:])
        prompt = memory + prompt

        threading.Thread(target=self.generate_for_nodes, args=(prompt, children, grandchildren)).start()

        # After asking for the generation, set loading text
        for child in children:
            child["text"] = "\n\n** Generating **"
        for grandchild in grandchildren:
            grandchild["text"] = "\n\n** Generating **"
        self.tree_updated()
        if update_selection:
            self.select_node(children[0]["id"])