-- Oracle sample DDL + PL/SQL for DCTE oracle-to-postgres plugin.

CREATE TABLE projects (
    project_id   VARCHAR2(32)   NOT NULL,
    project_name VARCHAR2(255)  NOT NULL,
    status       VARCHAR2(16)   DEFAULT 'DRAFT',
    budget       NUMBER(18,2),
    created_at   DATE           DEFAULT SYSDATE,
    approved_at  TIMESTAMP WITH LOCAL TIME ZONE,
    notes        CLOB,
    CONSTRAINT pk_projects PRIMARY KEY (project_id)
);

CREATE SEQUENCE seq_project_no START WITH 1000 INCREMENT BY 1;

CREATE OR REPLACE VIEW v_active_projects AS
SELECT project_id,
       project_name,
       NVL(status, 'DRAFT') AS status,
       DECODE(status, 'APPROVED', 1, 'REJECTED', -1, 0) AS status_flag
  FROM projects
 WHERE status <> 'ARCHIVED'
   AND ROWNUM <= 500;

CREATE OR REPLACE FUNCTION fn_next_project_no RETURN NUMBER IS
BEGIN
    RETURN seq_project_no.NEXTVAL;
END;
/

SELECT SYSDATE FROM DUAL;
