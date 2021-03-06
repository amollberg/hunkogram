#!/usr/bin/env python

# Hierarchy:
# AST
#  ._patches : Patch          .commitdiffs[] : CommitDiff (diff of one commit and its parent + metadata)
#   PatchHeader                 .header : Commit
#    _hash                        .hex
#   FilePatch                   .filepatches[] : Patch
#     FilePatchHeader             .delta : DiffDelta
#      _oldfile                     .old_file.path
#      _newfile                     .new_file.path
#     Fragment                    .hunks[] : DiffHunk
#      _content                     .lines
#      FragmentHeader
#       Range _oldrange
#        _start                     .old_start
#        _end                       .old_start + .old_lines
#       Range _newrange
#        _start                     .new_start
#        _end                       .new_start + .new_lines

import sys
import re
import pygit2
import json
import os

from .debug import *
from .commitdiff import CommitDiff

UNSTAGED_HEX = '0000000000000000000000000000000000000000'
STAGED_HEX = '0000000100000000000000000000000000000000'

def is_nullfile(fn):
  return fn == '/dev/null'

def nonnull_file(delta):
  if not is_nullfile(delta.old_file.path):
    return delta.old_file.path
  if not is_nullfile(delta.new_file.path):
    return delta.new_file.path
  # Both files are null files
  return None


class Range(object):
  _start = 0
  _end = 0

  def __init__(self, start, length):
    # Fix for inconvenient notation of empty lines
    # This eliminates the need for special cases in
    # some calculations.
    if length == 0:
      start += 1

    self._start = start
    self._end = start + length - 1

  def __repr__(self):
    return "<Range: %d to %d>" % (self._start, self._end,)

  def update_positions(self, start_delta, end_delta):
    self._start += start_delta
    self._end += end_delta

def oldrange(fragment):
  return Range(fragment.old_start, fragment.old_lines)

def newrange(fragment):
  return Range(fragment.new_start, fragment.new_lines)

def binary_range_length(file_path):
  if is_nullfile(file_path):
    return 0
  return 1

def binary_oldrange(patch):
  assert(patch.delta.is_binary)
  return Range(0, binary_range_length(patch.delta.old_file))

def binary_newrange(patch):
  assert(patch.delta.is_binary)
  return Range(0, binary_range_length(patch.delta.new_file))

def get_diff(repo, commit, find_similar=True):
  if isinstance(commit, pygit2.Commit):
    diff = repo.diff(commit.parents[0], commit, context_lines=0, interhunk_lines=0)
  else:
    diff = commit.get_diff(repo, context_lines=0, interhunk_lines=0)
  if find_similar:
    diff.find_similar()
  return diff

def hex_to_commit(repo, hex):
  if hex == STAGED_HEX:
    return Staged()
  if hex == UNSTAGED_HEX:
    return Unstaged()
  return repo[hex]

class BinaryLine(object):
  def __init__(self, content):
    self.origin = ''
    self.content = content

class BinaryHunk(object):
  def __init__(self, patch_that_is_binary):
    assert(patch_that_is_binary.delta.is_binary)
    delta = patch_that_is_binary.delta
    self.old_start = 0
    self.old_lines = binary_range_length(delta.old_file)
    self.new_start = 0
    self.new_lines = binary_range_length(delta.new_file)
    self.lines = [BinaryLine(line) for line in patch_that_is_binary.text.splitlines()]

class FakeCommit(object):
  def __init__(self, hex):
    self.hex = hex
    self.message = ''
    # Add more fields here as required

class Unstaged(FakeCommit):
  def __init__(self):
    super(Unstaged, self).__init__(UNSTAGED_HEX)
    self.message = ' (unstaged changes)'

  def get_diff(self, repo, **kwargs):
    return repo.diff(None, None, cached=False, **kwargs)

class Staged(FakeCommit):
  def __init__(self):
    super(Staged, self).__init__(STAGED_HEX)
    self.message = ' (staged changes)'

  def get_diff(self, repo, **kwargs):
    # This does NOT compare staged to HEAD
    # repo.diff(None, None, cached=True)
    return repo.index.diff_to_tree(repo.head.peel().tree, **kwargs)

class CommitSelection(object):
  def __init__(self, since_ref, until_ref, max_count, include_staged, include_unstaged):
    self.start = since_ref
    self.end = until_ref
    self.include_staged = include_staged
    self.include_unstaged = include_unstaged
    self.max_count = max_count

  def get_items(self, repo):
    print('... Finding commits            \r', end='')
    walker = repo.walk(repo.head.target,
                       pygit2.GIT_SORT_TOPOLOGICAL | pygit2.GIT_SORT_REVERSE)
    if self.end:
      walker.push(repo.revparse_single(self.end).hex)
    if self.start:
      walker.hide(repo.revparse_single(self.start).hex)
    if not (self.start or self.end):
      walker.hide(repo.revparse_single('HEAD~' + str(self.max_count)).hex)
    # Collect all selected commits
    commits = [commit for commit in walker]
    if self.max_count:
      # Limit the number of commits
      commits = commits[0:(self.max_count + 1)]

    def add_if_nonempty(commit):
      if len(commit.get_diff(repo)) > 0:
        commits.append(commit)

    if self.include_staged:
      print('... Retrieving staged changes  \r', end='')
      add_if_nonempty(Staged())
    if self.include_unstaged:
      print('... Retrieving unstaged changes\r', end='')
      add_if_nonempty(Unstaged())
    return commits

class ExplicitCommitSelection(object):
  def __init__(self, commit_hex_list):
    self.commit_hexes = commit_hex_list

  def get_items(self, repo):
    return [hex_to_commit(repo, hex) for hex in self.commit_hexes]

class CommitLoader(object):
  @staticmethod
  def load(repo_dir, commit_selection):
    repo_root = pygit2.discover_repository(repo_dir)
    if repo_root is None:
      raise RuntimeError('Error: Working directory is not a git repository.')
    repo = pygit2.Repository(repo_root)
    commits = commit_selection.get_items(repo)
    print('... Retrieving fragments       \r', end='')
    commitdiffs = [CommitDiff(commit, get_diff(repo, commit)) for commit in commits]
    print('                               \r', end='')
    return commitdiffs

class DictCoersionEncoder(json.JSONEncoder):
  def default(self, obj):
    try:
      return json.JSONEncoder.default(self, obj)
    except TypeError:
      return vars(obj)

def main():
  cl = CommitLoader()
  print(CommitLoader.load(os.getcwd(), CommitSelection('HEAD~4', None, 4, True, True)))

if __name__ == '__main__':
  debug.parse_args()
  main()
