-- ===========================================================================
-- One vacancy row per dedup key (FR-184)
--
-- ``vacancy.dedup_key`` is the posting's identity, and the writer looks it up
-- before it inserts.  The lookup and the insert are two transactions, and the
-- index under the key was a plain one, so nothing stopped a second row: an
-- installed database held 242 keys written twice, every pair from the same
-- adapter.  Each pair is one posting listed twice, and a campaign
-- that read both turned them into two identical opportunities.
--
-- The newest row of each key survives.  It is the one ``vacancy_by_dedup_key``
-- already returns, so every merge since the duplicate appeared has gone into
-- it and it holds the freshest fields.  The older rows are folded into it:
--
-- * an opportunity pointing at an older row moves to the surviving row, unless
--   the same campaign already has an opportunity for it
--   (``idx_opp_campaign_vacancy`` allows one).  That second
--   opportunity is a copy of the first, and it is deleted when nobody has
--   acted on it.  One the job seeker did act on is kept and only loses
--   its link to the deleted row.
-- * provenance moves with the row, so the record of where the posting came
--   from is not lost.  An embedding of a deleted row is dropped, because the
--   surviving row is embedded under its own id.
--
-- Then the unique index makes a second row impossible, and the writer turns
-- the resulting IntegrityError into a merge (pipeline/knowledge_base.py).
-- ===========================================================================

CREATE TEMP TABLE vacancy_dedup_fold AS
SELECT v.id AS old_id,
       (SELECT k.id FROM vacancy k
         WHERE k.dedup_key = v.dedup_key
         ORDER BY k.collected_at DESC, k.id DESC LIMIT 1) AS keep_id
  FROM vacancy v
 WHERE v.dedup_key IN (SELECT dedup_key FROM vacancy
                        WHERE dedup_key IS NOT NULL
                        GROUP BY dedup_key HAVING COUNT(*) > 1);

DELETE FROM vacancy_dedup_fold WHERE old_id = keep_id;

-- The opportunities that duplicate one the surviving row already has.
CREATE TEMP TABLE vacancy_dedup_twin AS
SELECT o.id AS opportunity_id
  FROM opportunity o
  JOIN vacancy_dedup_fold f ON f.old_id = o.vacancy_id
 WHERE EXISTS (SELECT 1 FROM opportunity k
                WHERE k.vacancy_id = f.keep_id AND k.campaign_id = o.campaign_id);

-- A twin nobody acted on: no status, selection, pin or rank, and nothing
-- built on it.  The child tables cascade, so this is checked, not assumed.
DELETE FROM opportunity
 WHERE id IN (SELECT opportunity_id FROM vacancy_dedup_twin)
   AND user_status = 'new' AND selected = 0 AND pinned = 0 AND manual_rank IS NULL
   AND id NOT IN (SELECT opportunity_id FROM application_package WHERE opportunity_id IS NOT NULL)
   AND id NOT IN (SELECT opportunity_id FROM pipeline_card WHERE opportunity_id IS NOT NULL)
   AND id NOT IN (SELECT opportunity_id FROM apply_selection WHERE opportunity_id IS NOT NULL)
   AND id NOT IN (SELECT opportunity_id FROM introduction_path WHERE opportunity_id IS NOT NULL)
   AND id NOT IN (SELECT opportunity_id FROM mock_interview_session WHERE opportunity_id IS NOT NULL)
   AND id NOT IN (SELECT opportunity_id FROM negotiation_brief WHERE opportunity_id IS NOT NULL)
   AND id NOT IN (SELECT opportunity_id FROM interview_appointment WHERE opportunity_id IS NOT NULL)
   AND id NOT IN (SELECT opportunity_id FROM manual_response WHERE opportunity_id IS NOT NULL);

DELETE FROM embedding
 WHERE entity_type = 'opportunity'
   AND entity_id IN (SELECT opportunity_id FROM vacancy_dedup_twin)
   AND entity_id NOT IN (SELECT id FROM opportunity);

-- Every other opportunity of an older row follows it to the surviving row.
-- A twin someone acted on stays where it is and loses the link on delete.
UPDATE opportunity
   SET vacancy_id = (SELECT keep_id FROM vacancy_dedup_fold WHERE old_id = opportunity.vacancy_id)
 WHERE vacancy_id IN (SELECT old_id FROM vacancy_dedup_fold)
   AND id NOT IN (SELECT opportunity_id FROM vacancy_dedup_twin);

UPDATE provenance
   SET entity_id = (SELECT keep_id FROM vacancy_dedup_fold WHERE old_id = provenance.entity_id)
 WHERE entity_type = 'vacancy'
   AND entity_id IN (SELECT old_id FROM vacancy_dedup_fold);

DELETE FROM embedding
 WHERE entity_type = 'vacancy'
   AND entity_id IN (SELECT old_id FROM vacancy_dedup_fold);

DELETE FROM vacancy WHERE id IN (SELECT old_id FROM vacancy_dedup_fold);

DROP TABLE vacancy_dedup_twin;
DROP TABLE vacancy_dedup_fold;

DROP INDEX IF EXISTS idx_vacancy_dedup;
CREATE UNIQUE INDEX idx_vacancy_dedup ON vacancy(dedup_key) WHERE dedup_key IS NOT NULL;
