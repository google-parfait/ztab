# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Calendar scenario test data.

Participant schedules and expected overlaps for the ScheduleOverlap policy.
"""

PARTICIPANT_A_SLOTS = [
    "2026-07-15T10:00:00Z",
    "2026-07-15T14:00:00Z",
    "2026-07-16T09:00:00Z",
]

PARTICIPANT_B_SLOTS = [
    "2026-07-15T14:00:00Z",
    "2026-07-15T16:00:00Z",
    "2026-07-16T09:00:00Z",
]

EXPECTED_OVERLAP = [
    "2026-07-15T14:00:00Z",
    "2026-07-16T09:00:00Z",
]
