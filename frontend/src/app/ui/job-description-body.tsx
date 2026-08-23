import styles from "@/app/dashboard.module.css";

export default function JobDescriptionBody({
  html,
  text,
}: {
  html: string | null;
  text: string | null;
}) {
  if (html) {
    return (
      <div
        className={styles.jobDescriptionHtml}
        dangerouslySetInnerHTML={{ __html: html }}
      />
    );
  }

  return (
    <div className={styles.resumeDocument}>
      {text || "No job description was stored for this posting."}
    </div>
  );
}
