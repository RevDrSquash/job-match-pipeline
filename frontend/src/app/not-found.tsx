import Link from "next/link";

import styles from "@/app/dashboard.module.css";

export default function NotFound() {
  return (
    <main className={styles.narrowShell}>
      <div className={styles.errorState}>
        <div>
          <span className={styles.emptyIcon}>?</span>
          <h2>That application was not found</h2>
          <p>It may not have finished generating, or the link is no longer valid.</p>
          <Link className={styles.button} href="/">
            Return to matches
          </Link>
        </div>
      </div>
    </main>
  );
}
