# Test Corpus Analysis

Companion to `METHODOLOGY.md`. This validates a single assumption behind the evaluation: that we
can populate the corpus with real, live repos that fit the seven patterns and whose backing
technologies are recoverable from the repo itself. Each candidate was vetted on GitHub for
popularity, current activity, pattern fit, and whether its backing services are evident from
compose files, manifests, or deployment artifacts.

## Candidates by pattern

Every pattern has a popular, currently maintained candidate. "Maps to" shows current resource
types; "gap" marks a detected technology with no resource type yet, which is a finding the
evaluation should surface, not a disqualifier.

| Pattern | Candidate | Stars | Detected tech | Maps to / gap |
|---|---|---|---|---|
| Web App | calcom/cal.com | 46k | Postgres | `Radius.Data/postgreSqlDatabases` |
| Microservices | GoogleCloudPlatform/microservices-demo | 20k | Redis; gRPC between services | redis maps; gRPC routing is a gap |
| Enterprise | spring-projects/spring-petclinic | 9.3k | MySQL + Postgres | `Radius.Data/mySqlDatabases`, `Radius.Data/postgreSqlDatabases` |
| Real-time | RocketChat/Rocket.Chat | 46k | MongoDB | `Applications.Datastores/mongoDatabases` |
| Data Pipeline | mage-ai/mage-ai | 8.7k | Postgres/pgvector; Spark | postgres maps; Spark is a gap |
| AI/ML | langgenius/dify | 146k | Postgres, Redis, vector DBs | postgres/redis map; vector DBs + model APIs are gaps |
| IoT | thingsboard/thingsboard | 22k | Postgres/Cassandra; MQTT | postgres maps; Cassandra + MQTT broker are gaps |
| Data Pipeline (Helm-primary) | apache/airflow | 46k | Postgres, Redis; in-repo Helm `chart/` | postgres/redis map; chart declares the topology |
| Web App (bare source) | gothinkster/django-realworld-example-app | 1.6k | Django + relational DB; no Docker/compose/chart | maps to a relational store; everything inferred from source |

## Diversity axes

The corpus should vary along more than just pattern, because the skill's task changes shape with
the inputs it is given.

Containerization maturity. A real value of the skill is containerizing apps that are not already
containerized, so the corpus should not consist only of repos that ship Dockerfiles. It should
span per-service Dockerfiles (microservices-demo, dify), a single root Dockerfile (cal.com, mage),
buildpacks with no Dockerfile (spring-petclinic builds its image via Spring buildpacks), and bare
source with no container setup at all (django-realworld ships only source and requirements). If
every repo ships a Dockerfile and compose, we only test translating an existing container topology
and never the harder, more valuable case of inferring containerization from source.

Deployment-definition format. The skill translates whatever deployment artifact exists, or none,
into Radius Bicep, and the starting format changes the work substantially: translating a Helm
chart that already declares services, dependencies, and values is very different from inferring
everything from bare source. The corpus should span bare source, compose only (cal.com, dify,
RocketChat, mage, thingsboard), plain k8s manifests (spring-petclinic, microservices-demo),
Kustomize (microservices-demo), Helm (apache/airflow ships an in-repo chart; microservices-demo
in-repo; RocketChat, dify, and thingsboard have charts in separate repos), bare source
(django-realworld), and Terraform plus service mesh (microservices-demo).
Today this is covered mostly because microservices-demo is a kitchen sink; real diversity means
assigning different primary formats to different repos rather than leaning on one.

## Findings

1. Backing technologies are recoverable across the corpus, though not always from a single root
   compose file. Some repos split services across subdirectories, app config, or IaC, so the
   evaluation must look beyond one file to establish what a repo really uses.
2. At least one datastore per pattern maps to an existing resource type. The remaining gaps cluster
   in messaging and specialty stores (gRPC routing, Spark, Cassandra, MQTT brokers, vector DBs,
   model APIs) and concentrate in Data Pipeline, AI/ML, and IoT. Surfacing these gaps is one of the
   evaluation's most valuable outputs.
3. Choosing popular repos means large, multi-service platforms rather than small focused apps. Each
   repo touches several technologies, so each is assigned one primary pattern and scored against it.
4. Containerization maturity and deployment-definition format are real diversity axes that change
   the skill's task, and the corpus should be balanced across them, not just across patterns.
