"""PoC ESCO seed: common software-engineering skills plus aliases.

Full ESCO (~13.9k) is the target taxonomy (docs/OPEN_ISSUES.md §6). The linker
interface is stable; swapping this seed for a downloaded ESCO CSV is a data
change, not a call-site change.

IDs are `esco:<slug>` placeholders until the official concept URIs are loaded.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillConcept:
    skill_id: str
    label: str
    aliases: tuple[str, ...] = ()


# (id slug, preferred label, aliases)
_SEED: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("python", "Python", ("python3", "python 3", "cpython")),
    ("javascript", "JavaScript", ("js", "ecmascript", "vanilla js")),
    ("typescript", "TypeScript", ("ts",)),
    ("java", "Java", ()),
    ("go", "Go", ("golang",)),
    ("rust", "Rust", ()),
    ("ruby", "Ruby", ()),
    ("php", "PHP", ()),
    ("csharp", "C#", ("c sharp", "c-sharp", "dotnet", ".net")),
    ("cplusplus", "C++", ("cpp", "c plus plus")),
    ("c", "C", ()),
    ("swift", "Swift", ()),
    ("kotlin", "Kotlin", ()),
    ("scala", "Scala", ()),
    ("r", "R", ()),
    ("sql", "SQL", ()),
    ("postgresql", "PostgreSQL", ("postgres", "psql", "pg")),
    ("mysql", "MySQL", ()),
    ("sqlite", "SQLite", ()),
    ("mongodb", "MongoDB", ("mongo",)),
    ("redis", "Redis", ()),
    ("elasticsearch", "Elasticsearch", ("elastic search", "opensearch")),
    ("dynamodb", "DynamoDB", ("dynamo db",)),
    ("cassandra", "Cassandra", ()),
    ("graphql", "GraphQL", ()),
    ("rest-api", "REST API", ("rest", "restful", "rest apis", "restful apis")),
    ("grpc", "gRPC", ("grpc api",)),
    ("html", "HTML", ("html5",)),
    ("css", "CSS", ("css3",)),
    ("react", "React", ("reactjs", "react.js")),
    ("vue", "Vue.js", ("vue", "vuejs")),
    ("angular", "Angular", ("angularjs",)),
    ("nextjs", "Next.js", ("nextjs", "next js")),
    ("nodejs", "Node.js", ("node", "nodejs", "node js")),
    ("django", "Django", ()),
    ("flask", "Flask", ()),
    ("fastapi", "FastAPI", ("fast api",)),
    ("rails", "Ruby on Rails", ("rails", "ror")),
    ("spring", "Spring", ("spring boot", "springboot")),
    ("express", "Express.js", ("express", "expressjs")),
    ("pandas", "pandas", ()),
    ("numpy", "NumPy", ("numpy",)),
    ("pytorch", "PyTorch", ("torch",)),
    ("tensorflow", "TensorFlow", ("tf",)),
    ("scikit-learn", "scikit-learn", ("sklearn", "scikit learn")),
    ("docker", "Docker", ("containers", "containerization")),
    ("kubernetes", "Kubernetes", ("k8s", "k8", "kube")),
    ("terraform", "Terraform", ()),
    ("ansible", "Ansible", ()),
    ("aws", "Amazon Web Services", ("aws", "amazon web services")),
    ("gcp", "Google Cloud Platform", ("gcp", "google cloud", "google cloud platform")),
    ("azure", "Microsoft Azure", ("azure", "ms azure")),
    ("linux", "Linux", ("unix",)),
    ("git", "Git", ()),
    ("github-actions", "GitHub Actions", ("github actions", "gh actions")),
    ("gitlab-ci", "GitLab CI", ("gitlab ci", "gitlab ci/cd")),
    ("ci-cd", "CI/CD", ("cicd", "continuous integration", "continuous delivery")),
    ("pytest", "pytest", ()),
    ("playwright", "Playwright", ()),
    ("selenium", "Selenium", ()),
    ("spark", "Apache Spark", ("spark", "pyspark")),
    ("airflow", "Apache Airflow", ("airflow",)),
    ("kafka", "Apache Kafka", ("kafka",)),
    ("rabbitmq", "RabbitMQ", ()),
    ("celery", "Celery", ()),
    ("alembic", "Alembic", ()),
    ("sqlalchemy", "SQLAlchemy", ()),
    ("huggingface", "Hugging Face", ("hugging face", "transformers")),
    ("langchain", "LangChain", ()),
    ("openai-api", "OpenAI API", ("openai",)),
    ("llm", "Large language models", ("llms", "large language model", "foundation models")),
    ("prompt-engineering", "Prompt engineering", ()),
    ("rag", "Retrieval-augmented generation", ("rag", "retrieval augmented generation")),
    ("vector-databases", "Vector databases", ("pgvector", "vector db", "vector database")),
    ("system-design", "System design", ("distributed systems", "systems design")),
    ("microservices", "Microservices", ("microservice",)),
    ("observability", "Observability", ("monitoring", "prometheus", "grafana", "opentelemetry")),
    ("security", "Application security", ("appsec", "application security", "owasp")),
    ("testing", "Software testing", ("unit testing", "integration testing", "tdd")),
    ("agile", "Agile", ("scrum", "kanban")),
    ("mentoring", "Mentoring", ("mentorship", "coaching engineers")),
    ("technical-leadership", "Technical leadership", ("tech lead", "technical lead", "team lead")),
    ("code-review", "Code review", ("code reviews", "pull request review")),
    ("documentation", "Technical documentation", ("technical writing",)),
    ("communication", "Communication", ("written communication", "verbal communication")),
    ("teamwork", "Teamwork", ("collaboration", "worked closely across teams", "cross-functional")),
    ("problem-solving", "Problem solving", ()),
    ("data-modeling", "Data modeling", ("data modelling", "schema design")),
    ("etl", "ETL", ("elt", "data pipelines", "data pipeline")),
    ("machine-learning", "Machine learning", ("ml", "machine-learning")),
    ("data-analysis", "Data analysis", ("data analytics",)),
    ("product-management", "Product management", ("product manager",)),
    ("project-management", "Project management", ()),
    ("excel", "Microsoft Excel", ("excel", "spreadsheets")),
    ("jira", "Jira", ()),
    ("figma", "Figma", ()),
    ("webpack", "webpack", ()),
    ("vite", "Vite", ()),
    ("s3", "Amazon S3", ("s3", "aws s3")),
    ("lambda", "AWS Lambda", ("lambda", "aws lambda")),
    ("ec2", "Amazon EC2", ("ec2",)),
    ("rds", "Amazon RDS", ("rds",)),
    ("cloudformation", "AWS CloudFormation", ("cloudformation",)),
    ("helm", "Helm", ()),
    ("nginx", "Nginx", ()),
    ("bash", "Bash", ("shell scripting", "bash scripting")),
)


def seed_concepts() -> tuple[SkillConcept, ...]:
    return tuple(
        SkillConcept(skill_id=f"esco:{slug}", label=label, aliases=aliases)
        for slug, label, aliases in _SEED
    )
