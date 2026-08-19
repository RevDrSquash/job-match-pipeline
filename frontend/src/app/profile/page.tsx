import type { Metadata } from "next";

import ProfileEditor from "@/app/ui/profile-editor";

export const metadata: Metadata = {
  title: "Profile · Job Match",
};

export default function ProfilePage() {
  return <ProfileEditor />;
}
