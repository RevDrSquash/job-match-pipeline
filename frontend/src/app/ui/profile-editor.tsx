"use client";

import { useEffect, useState } from "react";

import styles from "@/app/dashboard.module.css";
import { fetchProfile, fetchUsers, updateProfile } from "@/lib/api";
import { skillDisplayLabel } from "@/lib/skills";
import type {
  Profile,
  User,
  WorkBullet,
  WorkHistoryEntry,
} from "@/lib/types";

const ARRANGEMENTS = ["remote", "hybrid", "onsite"] as const;
const SENIORITY = [
  "",
  "intern",
  "junior",
  "mid",
  "senior",
  "staff",
  "principal",
  "lead",
] as const;

function listValue(values: string[] | null | undefined) {
  return (values ?? []).join(", ");
}

function parseList(value: string) {
  return value
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
}

function bulletText(bullet: WorkBullet | string) {
  return typeof bullet === "string" ? bullet : (bullet.text ?? "");
}

function ProfileContent({
  profile,
  onSaved,
}: {
  profile: Profile;
  onSaved: (profile: Profile) => void;
}) {
  const [history, setHistory] = useState<WorkHistoryEntry[]>(profile.work_history);
  const [skills, setSkills] = useState(listValue(profile.skill_ids));
  const [titles, setTitles] = useState(listValue(profile.filters.title_families));
  const [locations, setLocations] = useState(listValue(profile.filters.locations));
  const [seniority, setSeniority] = useState(profile.filters.seniority_band ?? "");
  const [compFloor, setCompFloor] = useState(
    profile.filters.comp_floor?.toString() ?? "",
  );
  const [arrangements, setArrangements] = useState<string[]>(
    profile.filters.work_arrangement ?? [],
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const updateRole = (
    index: number,
    key: keyof WorkHistoryEntry,
    value: unknown,
  ) => {
    setHistory((current) =>
      current.map((entry, entryIndex) =>
        entryIndex === index ? { ...entry, [key]: value } : entry,
      ),
    );
  };

  const addRole = () => {
    setHistory((current) => [
      ...current,
      {
        employer: "",
        title: "",
        start_date: null,
        end_date: null,
        is_current: false,
        location: null,
        source: "user_asserted",
        bullets: [],
      },
    ]);
  };

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      const parsedComp = compFloor.trim() ? Number(compFloor) : null;
      if (parsedComp !== null && (!Number.isFinite(parsedComp) || parsedComp < 0)) {
        throw new Error("Compensation floor must be a positive number.");
      }

      const result = await updateProfile({
        user_id: profile.user_id,
        work_history: history,
        skill_ids: parseList(skills),
        title_families: parseList(titles),
        locations: parseList(locations),
        work_arrangement: arrangements,
        seniority_band: seniority || null,
        ...(parsedComp === null
          ? { clear_comp_floor: true }
          : { comp_floor: Math.round(parsedComp), clear_comp_floor: false }),
      });
      onSaved(result);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Profile changes could not be saved.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      {profile.rescan_message && (
        <div className={styles.rescanBanner} role="status">
          <div>
            <strong>Changes saved</strong>
            <span>{profile.rescan_message}</span>
          </div>
          <span>Profile v{profile.profile_version}</span>
        </div>
      )}

      <div className={styles.profileGrid}>
        <section className={styles.panel}>
          <div className={styles.panelHeader}>
            <div>
              <h2>Work history</h2>
              <span>Correct anything the parser got wrong.</span>
            </div>
            <button className={styles.button} type="button" onClick={addRole}>
              Add role
            </button>
          </div>
          <div className={styles.panelBody}>
            <div className={styles.fields}>
              {history.map((role, index) => (
                <div className={styles.panel} key={index}>
                  <div className={styles.panelBody}>
                    <div className={styles.fields}>
                      <div className={styles.buttonRow}>
                        <div className={styles.field}>
                          <label htmlFor={`role-title-${index}`}>Title</label>
                          <input
                            id={`role-title-${index}`}
                            value={String(role.title ?? "")}
                            onChange={(event) =>
                              updateRole(index, "title", event.target.value)
                            }
                          />
                        </div>
                        <div className={styles.field}>
                          <label htmlFor={`role-employer-${index}`}>Employer</label>
                          <input
                            id={`role-employer-${index}`}
                            value={String(role.employer ?? "")}
                            onChange={(event) =>
                              updateRole(index, "employer", event.target.value)
                            }
                          />
                        </div>
                      </div>
                      <div className={styles.buttonRow}>
                        <div className={styles.field}>
                          <label htmlFor={`role-start-${index}`}>Start date</label>
                          <input
                            id={`role-start-${index}`}
                            placeholder="YYYY-MM"
                            value={String(role.start_date ?? "")}
                            onChange={(event) =>
                              updateRole(
                                index,
                                "start_date",
                                event.target.value || null,
                              )
                            }
                          />
                        </div>
                        <div className={styles.field}>
                          <label htmlFor={`role-end-${index}`}>End date</label>
                          <input
                            id={`role-end-${index}`}
                            placeholder="Current"
                            value={String(role.end_date ?? "")}
                            onChange={(event) => {
                              updateRole(
                                index,
                                "end_date",
                                event.target.value || null,
                              );
                              updateRole(index, "is_current", !event.target.value);
                            }}
                          />
                        </div>
                        <div className={styles.field}>
                          <label htmlFor={`role-location-${index}`}>Location</label>
                          <input
                            id={`role-location-${index}`}
                            value={String(role.location ?? "")}
                            onChange={(event) =>
                              updateRole(
                                index,
                                "location",
                                event.target.value || null,
                              )
                            }
                          />
                        </div>
                      </div>
                      <div className={styles.field}>
                        <label htmlFor={`role-bullets-${index}`}>Highlights</label>
                        <textarea
                          id={`role-bullets-${index}`}
                          value={(role.bullets ?? []).map(bulletText).join("\n")}
                          onChange={(event) =>
                            updateRole(
                              index,
                              "bullets",
                              event.target.value
                                .split("\n")
                                .map((line) => line.trim())
                                .filter(Boolean),
                            )
                          }
                        />
                        <span className={styles.fieldHint}>
                          One grounded accomplishment per line. Manual edits are
                          stored as user-asserted.
                        </span>
                      </div>
                      <button
                        className={`${styles.button} ${styles.dangerButton}`}
                        type="button"
                        onClick={() =>
                          setHistory((current) =>
                            current.filter((_, entryIndex) => entryIndex !== index),
                          )
                        }
                      >
                        Remove role
                      </button>
                    </div>
                  </div>
                </div>
              ))}
              {history.length === 0 && (
                <p className={styles.fieldHint}>
                  No work history is stored. Add a role to correct the profile.
                </p>
              )}
            </div>
          </div>
        </section>

        <div>
          <section className={styles.panel}>
            <div className={styles.panelHeader}>
              <div>
                <h2>Match filters</h2>
                <span>Keep these broad enough to discover adjacent roles.</span>
              </div>
            </div>
            <div className={styles.panelBody}>
              <div className={styles.fields}>
                <div className={styles.field}>
                  <label htmlFor="profile-titles">Title families</label>
                  <input
                    id="profile-titles"
                    value={titles}
                    onChange={(event) => setTitles(event.target.value)}
                    placeholder="Backend Engineering, Platform"
                  />
                  <span className={styles.fieldHint}>Comma separated</span>
                </div>
                <div className={styles.field}>
                  <label htmlFor="profile-locations">Locations</label>
                  <input
                    id="profile-locations"
                    value={locations}
                    onChange={(event) => setLocations(event.target.value)}
                    placeholder="Remote, Vancouver"
                  />
                  <span className={styles.fieldHint}>Comma separated</span>
                </div>
                <div className={styles.field}>
                  <span className={styles.fieldLabel}>Work arrangement</span>
                  <div className={styles.checkGrid}>
                    {ARRANGEMENTS.map((item) => (
                      <label className={styles.checkOption} key={item}>
                        <input
                          type="checkbox"
                          checked={arrangements.includes(item)}
                          onChange={(event) =>
                            setArrangements((current) =>
                              event.target.checked
                                ? [...current, item]
                                : current.filter((value) => value !== item),
                            )
                          }
                        />
                        {item}
                      </label>
                    ))}
                  </div>
                </div>
                <div className={styles.field}>
                  <label htmlFor="profile-seniority">Seniority</label>
                  <select
                    id="profile-seniority"
                    value={seniority}
                    onChange={(event) => setSeniority(event.target.value)}
                  >
                    {SENIORITY.map((value) => (
                      <option key={value || "any"} value={value}>
                        {value || "Any seniority"}
                      </option>
                    ))}
                  </select>
                </div>
                <div className={styles.field}>
                  <label htmlFor="profile-comp">Compensation floor (USD)</label>
                  <input
                    id="profile-comp"
                    inputMode="numeric"
                    min="0"
                    type="number"
                    value={compFloor}
                    onChange={(event) => setCompFloor(event.target.value)}
                    placeholder="No minimum"
                  />
                </div>
              </div>
            </div>
          </section>

          <section className={`${styles.panel} ${styles.skillsPanel}`}>
            <div className={styles.panelHeader}>
              <div>
                <h2>Profile representation</h2>
                <span>Used to rank jobs against your experience.</span>
              </div>
            </div>
            <div className={styles.panelBody}>
              <div className={styles.fields}>
                {(profile.skills ?? []).length > 0 && (
                  <div className={styles.field}>
                    <span className={styles.fieldLabel}>Linked skills</span>
                    <div className={styles.chips}>
                      {(profile.skills ?? []).map((skill) => (
                        <span className={styles.chip} key={skill.id}>
                          {skillDisplayLabel(skill)}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
                <div className={styles.field}>
                  <label htmlFor="profile-skills">Canonical skill IDs</label>
                  <textarea
                    className={styles.monospace}
                    id="profile-skills"
                    value={skills}
                    onChange={(event) => setSkills(event.target.value)}
                  />
                  <span className={styles.fieldHint}>Comma separated ESCO IDs</span>
                </div>
                <div className={styles.field}>
                  <span className={styles.fieldLabel}>Synthesized profile</span>
                  <div className={styles.profileSummary}>
                    {profile.synthesized_doc || "No synthesized profile is stored."}
                  </div>
                  <span className={styles.fieldHint}>
                    Rebuilt from your grounded work history when you save.
                  </span>
                </div>
              </div>
            </div>
          </section>
        </div>
      </div>

      <div className={styles.profileActions}>
        {error && <span className={styles.dangerButton}>{error}</span>}
        <button
          className={`${styles.button} ${styles.primaryButton}`}
          disabled={saving}
          onClick={() => void save()}
        >
          {saving ? "Saving…" : "Save profile changes"}
        </button>
      </div>
    </>
  );
}

export default function ProfileEditor() {
  const [users, setUsers] = useState<User[]>([]);
  const [userId, setUserId] = useState("");
  const [profile, setProfile] = useState<Profile | null>(null);
  const [loadingUsers, setLoadingUsers] = useState(true);
  const [loadedUserId, setLoadedUserId] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    fetchUsers()
      .then((rows) => {
        if (!active) return;
        setUsers(rows);
        if (rows.length === 1) setUserId(rows[0].id);
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setError(reason instanceof Error ? reason.message : "Unable to load users.");
      })
      .finally(() => {
        if (active) setLoadingUsers(false);
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!userId) return;
    let active = true;
    fetchProfile(userId)
      .then((result) => {
        if (!active) return;
        setProfile(result);
        setLoadedUserId(userId);
        setError("");
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setError(reason instanceof Error ? reason.message : "Unable to load the profile.");
        setLoadedUserId(userId);
      });
    return () => {
      active = false;
    };
  }, [userId]);

  const loading = loadingUsers || Boolean(userId && loadedUserId !== userId);

  return (
    <main className={styles.shell}>
      <div className={styles.pageHeader}>
        <div>
          <p className={styles.eyebrow}>Your source of truth</p>
          <h1>Keep the match profile honest and current.</h1>
          <p>
            Review the parsed experience that grounds generated resumes, then
            tune the filters that decide which roles enter your feed.
          </p>
        </div>
        {users.length > 1 && (
          <div className={styles.userSelect}>
            <label htmlFor="profile-user">Profile</label>
            <select
              id="profile-user"
              value={userId}
              onChange={(event) => setUserId(event.target.value)}
            >
              <option value="">Choose a profile</option>
              {users.map((user, index) => (
                <option key={user.id} value={user.id}>
                  Profile {index + 1} · {user.tier}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>

      {loading ? (
        <div className={styles.loadingState}>
          <div>
            <div className={styles.spinner} />
            <p>Loading profile…</p>
          </div>
        </div>
      ) : error ? (
        <div className={styles.errorState}>
          <div>
            <span className={styles.emptyIcon}>!</span>
            <h2>Profile unavailable</h2>
            <p>{error}</p>
          </div>
        </div>
      ) : users.length === 0 ? (
        <div className={styles.emptyState}>
          <div>
            <span className={styles.emptyIcon}>＋</span>
            <h2>No profile yet</h2>
            <p>Use the profile ingestion CLI, then return here to review it.</p>
          </div>
        </div>
      ) : !userId ? (
        <div className={styles.emptyState}>
          <div>
            <span className={styles.emptyIcon}>↗</span>
            <h2>Choose a profile</h2>
            <p>Select a profile to review and edit.</p>
          </div>
        </div>
      ) : profile ? (
        <ProfileContent
          key={`${profile.user_id}-${profile.profile_version}`}
          profile={profile}
          onSaved={setProfile}
        />
      ) : null}
    </main>
  );
}
