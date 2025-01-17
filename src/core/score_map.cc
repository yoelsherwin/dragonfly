// Copyright 2023, DragonflyDB authors.  All rights reserved.
// See LICENSE for licensing terms.
//

#include "core/score_map.h"

#include "base/endian.h"
#include "base/logging.h"
#include "core/compact_object.h"
#include "core/sds_utils.h"

extern "C" {
#include "redis/zmalloc.h"
}

using namespace std;

namespace dfly {

namespace {

inline double GetValue(sds key) {
  char* valptr = key + sdslen(key) + 1;
  return absl::bit_cast<double>(absl::little_endian::Load64(valptr));
}

void* AllocateScored(string_view field, double value) {
  size_t meta_offset = field.size() + 1;

  // The layout is:
  // key, '\0', 8-byte double value
  sds newkey = AllocSdsWithSpace(field.size(), 8);

  if (!field.empty()) {
    memcpy(newkey, field.data(), field.size());
  }

  absl::little_endian::Store64(newkey + meta_offset, absl::bit_cast<uint64_t>(value));

  return newkey;
}

}  // namespace

ScoreMap::~ScoreMap() {
  Clear();
}

pair<void*, bool> ScoreMap::AddOrUpdate(string_view field, double value) {
  void* newkey = AllocateScored(field, value);

  // Replace the whole entry.
  sds prev_entry = (sds)AddOrReplaceObj(newkey, false);
  if (prev_entry) {
    ObjDelete(prev_entry, false);
    return {newkey, false};
  }

  return {newkey, true};
}

std::pair<void*, bool> ScoreMap::AddOrSkip(std::string_view field, double value) {
  void* obj = FindInternal(&field, 1);  // 1 - string_view

  if (obj)
    return {obj, false};

  return AddOrUpdate(field, value);
}

bool ScoreMap::Erase(string_view key) {
  return EraseInternal(&key, 1);
}

void ScoreMap::Clear() {
  ClearInternal();
}

std::optional<double> ScoreMap::Find(std::string_view key) {
  sds str = (sds)FindInternal(&key, 1);
  if (!str)
    return nullopt;

  return GetValue(str);
}

uint64_t ScoreMap::Hash(const void* obj, uint32_t cookie) const {
  DCHECK_LT(cookie, 2u);

  if (cookie == 0) {
    sds s = (sds)obj;
    return CompactObj::HashCode(string_view{s, sdslen(s)});
  }

  const string_view* sv = (const string_view*)obj;
  return CompactObj::HashCode(*sv);
}

bool ScoreMap::ObjEqual(const void* left, const void* right, uint32_t right_cookie) const {
  DCHECK_LT(right_cookie, 2u);

  sds s1 = (sds)left;
  if (right_cookie == 0) {
    sds s2 = (sds)right;

    if (sdslen(s1) != sdslen(s2)) {
      return false;
    }

    return sdslen(s1) == 0 || memcmp(s1, s2, sdslen(s1)) == 0;
  }

  const string_view* right_sv = (const string_view*)right;
  string_view left_sv{s1, sdslen(s1)};
  return left_sv == (*right_sv);
}

size_t ScoreMap::ObjectAllocSize(const void* obj) const {
  sds s1 = (sds)obj;
  size_t res = zmalloc_usable_size(sdsAllocPtr(s1));
  return res;
}

uint32_t ScoreMap::ObjExpireTime(const void* obj) const {
  // Should not reach.
  return UINT32_MAX;
}

void ScoreMap::ObjDelete(void* obj, bool has_ttl) const {
  sds s1 = (sds)obj;
  sdsfree(s1);
}

detail::SdsScorePair ScoreMap::iterator::BreakToPair(void* obj) {
  sds f = (sds)obj;
  return detail::SdsScorePair(f, GetValue(f));
}

}  // namespace dfly
