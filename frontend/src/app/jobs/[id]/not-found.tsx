import Link from "next/link";

import styles from "@/app/dashboard.module.css";

export default function JobNotFound() {
  return (
    <main className={styles.narrowShell}>
      <div className={styles.errorState}>
        <div>
          <span className={styles.emptyIcon}>?</span>
          <h2>That job was not found</h2>
          <p>It may have been removed, or the link is no longer valid.</p>
          <Link className={styles.button} href="/jobs">
            Return to job search
          </Link>
        </div>
      </div>
    </main>
  );
}
