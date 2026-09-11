"""Smoke-test the tech detector — runs inside the container."""
import sys
sys.path.insert(0, "/app/backend")
from kb.tech_detector import detect_tech_stack

files = [
    {"id": "1", "filename": "src/main/java/com/acme/TenderController.java", "filetype": "java", "size": 5000},
    {"id": "2", "filename": "src/main/java/com/acme/TenderService.java",    "filetype": "java", "size": 6000},
    {"id": "3", "filename": "src/main/java/com/acme/TenderRepository.java", "filetype": "java", "size": 3000},
    {"id": "4", "filename": "src/main/webapp/WEB-INF/views/tender.jsp",     "filetype": "jsp",  "size": 2000},
    {"id": "5", "filename": "src/main/webapp/WEB-INF/web.xml",              "filetype": "config", "size": 1000},
    {"id": "6", "filename": "pom.xml",                                       "filetype": "config", "size": 4000},
    {"id": "7", "filename": "src/main/resources/applicationContext.xml",     "filetype": "config", "size": 2000},
    {"id": "8", "filename": "db/schema.sql",                                 "filetype": "sql",  "size": 8000},
]
chunks = {
    "1": ["package com.acme;\nimport org.springframework.web.bind.annotation.*;\n@RestController\npublic class TenderController { @GetMapping(\"/tender\") public String list() { return \"ok\"; } }"],
    "2": ["package com.acme;\nimport org.springframework.stereotype.Service;\nimport org.hibernate.SessionFactory;\n@Service\npublic class TenderService { }"],
    "3": ["package com.acme;\nimport javax.persistence.*;\n@Entity\npublic class Tender { @Id Long id; }"],
    "5": ["<web-app xmlns='http://java.sun.com/xml/ns/javaee'>\n<servlet>...</servlet>\n</web-app>"],
    "6": ["<project xmlns='http://maven.apache.org/POM/4.0.0'>\n<dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter</artifactId></dependency>\n<dependency><groupId>com.oracle.database.jdbc</groupId><artifactId>ojdbc8</artifactId></dependency></project>"],
    "8": ["-- jdbc:oracle:thin:@localhost:1521\nCREATE OR REPLACE PACKAGE tender_pkg AS\nFUNCTION calc_late_fee RETURN NUMBER;\nEND;\nCREATE TABLE tender (id NUMBER PRIMARY KEY, name NVARCHAR2(200));"],
}

t = detect_tech_stack(files, chunks)
print("=== DETECTED STACK ===")
for k in ("summary", "language", "languages", "frameworks", "database", "databases", "build"):
    print(f"  {k:11s}: {t[k]}")
print()
print("Expected:  Java / JSP / Spring Boot + Hibernate / Oracle / Maven")

